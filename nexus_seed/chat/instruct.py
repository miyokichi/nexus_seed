"""Project Instructions — telling one project what to do, from its own chat.

The read-only Project Chat explains a project; this is the other half: a
per-project channel for *acting* on it when something happens.  The pipeline
keeps the existing boundary rather than removing it::

    human instruction + project_id
        -> ProjectSituation projection
        -> an explicit Control Plane command (typed directly, or proposed by the LLM)
        -> deterministic scope + allow-list validation
        -> ConsoleService.execute            <- the only thing that changes state
        -> the same durable chat thread

Two rules make this safe, and neither depends on a model behaving well:

* **The LLM only proposes.** It never executes. Its output must parse as one
  explicit command, its verb must be allow-listed, and every identifier it names
  must already belong to *this* project — otherwise the instruction is refused.
* **Change still belongs to the Control Plane.** Nothing here writes Goal, Work,
  Event or World State directly; it calls the same authorized, audited
  ``ConsoleService`` the ``/control`` endpoint uses, as the same human identity.

An instruction typed as an explicit ``/command`` needs no LLM at all, so the
channel keeps working when no model is configured.
"""

from __future__ import annotations

from typing import Any

from ..backends.base import BackendRequest
from ..control.models import CommandStatus
from ..control.parser import CommandParseError, parse_explicit_command
from ..projects.projections import get_project_situation, get_project_summaries
from .guards import detect_foreign_projects
from .models import (
    ChatAnswerStatus,
    ChatCertainty,
    ChatRole,
    ProjectChatMessage,
)

#: The backend key the runtime registers a configured LLM under.
BACKEND_NAME = "llm"

#: How a command's target identifier must relate to this project.
#:   "work"    -> the id must be Work of this project
#:   "review"  -> the id must be a pending review of this project
#:   "goal"    -> the id must be this project's Goal
#:   "project" -> no target id; the project is supplied as an argument
ALLOWED_COMMANDS: dict[str, str] = {
    "task.create": "project",
    "work.show": "work",
    "work.pause": "work",
    "work.resume": "work",
    "work.cancel": "work",
    "work.priority": "work",
    "work.deadline": "work",
    "work.provider": "work",
    "work.context": "work",
    "work.trace": "work",
    "review.approve": "review",
    "review.reject": "review",
    "goal.show": "goal",
    "goal.pause": "goal",
    "goal.resume": "goal",
    "goal.cancel": "goal",
    "goal.evaluate": "goal",
}

INSTRUCTION = (
    "You turn one human instruction about a single project into exactly one "
    "explicit NEXUS SEED control command. "
    "Use only the identifiers listed in project_scope; never invent one. "
    "Available commands: "
    "/task <text> (add new work to this project), "
    "/pause|/resume|/cancel|/priority|/deadline|/provider <work-id> [value], "
    "/approve|/reject <review-id>, "
    "/goal pause|resume|cancel|evaluate <goal-id>. "
    "Set command to the full command string. "
    "If the instruction cannot be expressed as one of these commands over the "
    "listed identifiers, set command to null and explain why in reason. "
    "Never guess an identifier that is not in project_scope."
)

COMMAND_SCHEMA = {
    "type": "object",
    "required": ["command", "reason"],
    "properties": {
        "command": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
}

NO_LLM_ANSWER = (
    "LLMが未接続のため、自然文の指示をコマンドに変換できません。\n"
    "明示コマンドであればそのまま実行できます。例:\n"
    "  /task 地域別の内訳も出す\n"
    "  /pause <work-id>"
)


def project_scope(situation) -> dict[str, list[str]]:
    """Return the identifiers an instruction about this project may name.

    Everything outside this set is refused deterministically, so a model that
    invents or borrows an identifier cannot reach another project's records.
    """

    def ids(entries) -> list[str]:
        found = []
        for entry in entries or ():
            value = str((entry or {}).get("id") or "").strip()
            if value and value not in found:
                found.append(value)
        return found

    goals = ids(situation.active_goals)
    root = str((situation.goal or {}).get("id") or "").strip()
    if root and root not in goals:
        goals.insert(0, root)
    return {
        "work": ids(situation.active_work)
        + [i for i in ids(situation.blocked_work) if i not in ids(situation.active_work)]
        + [
            i
            for i in ids(situation.recently_completed_work)
            if i not in ids(situation.active_work) + ids(situation.blocked_work)
        ],
        "review": ids(situation.pending_reviews),
        "goal": goals,
    }


class ProjectInstructionService:
    """Executes a human instruction about one project, through the Control Plane."""

    def __init__(self, runtime, console, *, backend_name: str = BACKEND_NAME) -> None:
        self.runtime = runtime
        self.console = console
        self.backend_name = backend_name

    @property
    def available(self) -> bool:
        """Whether instructions can be executed at all (Control Plane present)."""
        return self.console is not None

    async def instruct(
        self,
        project_id: str,
        message: str,
        *,
        issuer_identity_id: str,
        source_channel: str = "cockpit-instruct",
    ) -> dict[str, Any] | None:
        """Carry out one instruction, or return ``None`` when the project is unknown."""

        text = (message or "").strip()
        if not text:
            raise ValueError("message must not be empty")
        situation = get_project_situation(self.runtime, project_id)
        if situation is None:
            return None

        thread = self.runtime.chat_store.ensure_thread(project_id)
        answer, status, metadata = await self._carry_out(
            situation,
            text,
            issuer_identity_id=issuer_identity_id,
            source_channel=source_channel,
        )

        question = self.runtime.chat_store.append(
            ProjectChatMessage(
                thread_id=thread.id,
                project_id=project_id,
                role=ChatRole.HUMAN,
                text=text,
                metadata={"kind": "INSTRUCTION"},
            )
        )
        reply = self.runtime.chat_store.append(
            ProjectChatMessage(
                thread_id=thread.id,
                project_id=project_id,
                role=ChatRole.NEXUS_SEED,
                text=answer,
                status=status,
                certainty=ChatCertainty.FACT,
                metadata=metadata,
            )
        )
        return {
            "project_id": project_id,
            "thread_id": str(thread.id),
            "answer": reply.text,
            "status": reply.status.value,
            "command": metadata.get("command"),
            "command_status": metadata.get("command_status"),
            "question_message_id": str(question.id),
            "answer_message_id": str(reply.id),
            "created_at": reply.created_at.isoformat(),
        }

    # --- deciding and running the command ---------------------------------

    async def _carry_out(
        self, situation, text: str, *, issuer_identity_id: str, source_channel: str
    ):
        project_id = situation.project_id
        if self.console is None:
            return (
                "Control Planeが無効のため、指示を実行できません。",
                ChatAnswerStatus.INSTRUCTION_REFUSED,
                {"reason": "control plane disabled"},
            )

        foreign = detect_foreign_projects(
            text,
            project_id=project_id,
            known_projects=get_project_summaries(self.runtime),
        )
        if foreign:
            named = "、".join(foreign)
            return (
                f"この指示欄は {project_id} にscopeされています。\n"
                f"{named} への指示はそのProjectの画面から出してください。",
                ChatAnswerStatus.OUT_OF_SCOPE,
                {"out_of_scope_projects": foreign},
            )

        scope = project_scope(situation)
        if text.startswith("/"):
            command_text, origin, reason = text, "explicit", ""
        else:
            command_text, reason = await self._propose(situation, text, scope)
            origin = "llm"
            if command_text is None:
                return (
                    reason or NO_LLM_ANSWER,
                    ChatAnswerStatus.INSTRUCTION_REFUSED,
                    {"reason": reason, "origin": origin},
                )

        return self._execute(
            command_text,
            scope=scope,
            project_id=project_id,
            origin=origin,
            proposal_reason=reason,
            issuer_identity_id=issuer_identity_id,
            source_channel=source_channel,
        )

    async def _propose(self, situation, text: str, scope: dict[str, list[str]]):
        """Ask the LLM for one explicit command.  Returns ``(command, reason)``."""

        backend = self.runtime.backends.get(self.backend_name)
        if backend is None:
            return None, NO_LLM_ANSWER

        request = BackendRequest(
            instruction=INSTRUCTION,
            context={
                "project_id": situation.project_id,
                "instruction": text,
                "project_scope": scope,
                "project_situation": situation.to_dict(),
            },
            output_schema=COMMAND_SCHEMA,
            metadata={"project_id": situation.project_id, "kind": "instruction"},
        )
        result = await backend.execute(request)
        if not result.success:
            return None, f"LLM呼び出しに失敗しました: {result.error}"

        output = result.parsed_output
        if not isinstance(output, dict):
            return None, "LLMの応答がコマンド形式ではありませんでした。"
        proposed = output.get("command")
        why = str(output.get("reason") or "")
        if not isinstance(proposed, str) or not proposed.strip():
            return None, why or "この指示は実行可能なコマンドに変換できませんでした。"
        return proposed.strip(), why

    def _execute(
        self,
        command_text: str,
        *,
        scope: dict[str, list[str]],
        project_id: str,
        origin: str,
        proposal_reason: str,
        issuer_identity_id: str,
        source_channel: str,
    ):
        """Validate a command against this project, then run it."""

        base = {"command": command_text, "origin": origin, "reason": proposal_reason}
        try:
            command = parse_explicit_command(
                command_text,
                issuer_identity_id=issuer_identity_id,
                source_channel=source_channel,
            )
        except CommandParseError as exc:
            return (
                f"コマンドとして解釈できませんでした: {exc}",
                ChatAnswerStatus.INSTRUCTION_REFUSED,
                {**base, "reason": str(exc)},
            )

        rejection = self._out_of_scope(command, scope=scope, project_id=project_id)
        if rejection is not None:
            return (
                rejection,
                ChatAnswerStatus.INSTRUCTION_REFUSED,
                {**base, "reason": rejection},
            )

        result = self.console.execute(command)
        succeeded = result.status is CommandStatus.EXECUTED
        metadata = {
            **base,
            "command_status": result.status.value,
            "command_id": str(result.command_id),
            "affected_entities": result.affected_entities,
        }
        if succeeded:
            summary = result.message or f"{command.command_type} を実行しました。"
            return f"実行しました。\n{summary}", ChatAnswerStatus.INSTRUCTION_EXECUTED, metadata
        detail = result.failure_reason or result.message or result.status.value
        return (
            f"実行できませんでした。\n{detail}",
            ChatAnswerStatus.INSTRUCTION_FAILED,
            metadata,
        )

    @staticmethod
    def _out_of_scope(command, *, scope: dict[str, list[str]], project_id: str) -> str | None:
        """Return why a command may not run here, or ``None`` when it may.

        This is the guarantee, not the prompt: an allow-listed verb whose target
        is not already part of this project is refused before execution.
        """

        kind = ALLOWED_COMMANDS.get(command.command_type)
        if kind is None:
            return (
                f"{command.command_type} はProject指示欄からは実行できません。"
                "Control Planeから実行してください。"
            )
        if kind == "project":
            # New work always belongs to the project the instruction came from.
            command.arguments["project"] = project_id
            return None
        target = str(command.target_id or "").strip()
        if not target:
            return f"{command.command_type} には対象IDが必要です。"
        if target not in scope.get(kind, []):
            return (
                f"{target} はこのProjectの{kind}ではありません。"
                "このProjectに属する対象のみ操作できます。"
            )
        return None


__all__ = [
    "ALLOWED_COMMANDS",
    "BACKEND_NAME",
    "COMMAND_SCHEMA",
    "INSTRUCTION",
    "NO_LLM_ANSWER",
    "ProjectInstructionService",
    "project_scope",
]
