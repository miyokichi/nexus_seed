"""Project Chat — asking about a project in words, and only asking.

The pipeline is deliberately short and one-directional::

    human message + project_id
        -> ProjectSituation projection
        -> project chat context
        -> LLM
        -> human-readable answer

Nothing in this module writes Event, World State, Goal, Intention, Work,
Process or Continuation records, and nothing here starts an Action, a Replan or
a Capability Acquisition.  The only durable writes are the two rows of the
conversation itself, in the project chat journal (Invariants 186–195).  A
request to change something is refused rather than routed, because the Control
Plane — not a chat answer — is where change belongs.
"""

from __future__ import annotations

from typing import Any

from ..backends.base import BackendRequest
from ..projects.projections import get_project_situation, get_project_summaries
from .context import (
    build_chat_context,
    deterministic_answer,
    known_references,
)
from .guards import detect_foreign_projects, detect_state_change_request
from .models import (
    ChatAnswerStatus,
    ChatCertainty,
    ChatRole,
    ProjectChatMessage,
)

#: The backend key the runtime registers a configured LLM under.
BACKEND_NAME = "llm"

INSTRUCTION = (
    "You are NEXUS SEED explaining one project to the human who owns it. "
    "Answer only from the supplied project_situation and recent_chat; they are "
    "the whole of what you know. Follow these rules without exception: "
    "(1) prefer facts stated by the projection; "
    "(2) say plainly that you do not know when the projection does not answer "
    "the question, and set certainty to UNKNOWN; "
    "(3) when you reason beyond the stated facts, say so and set certainty to "
    "INFERENCE; only a directly stated fact is FACT; "
    "(4) never mention or use information about any other project; "
    "(5) never change state, execute an action, replan or approve anything — "
    "you are read-only and must say so if asked to act; "
    "(6) write for a person: summarise instead of listing raw identifiers or "
    "internal process names; "
    "(7) put the identifiers that support the answer in references. "
    "Answer in the language of the question."
)

ANSWER_SCHEMA = {
    "type": "object",
    "required": ["answer", "certainty"],
    "properties": {
        "answer": {"type": "string", "minLength": 1},
        "certainty": {"enum": ["FACT", "INFERENCE", "UNKNOWN"]},
        "references": {"type": "array", "items": {"type": "string"}},
    },
}

READ_ONLY_ANSWER = (
    "この依頼はProject状態の変更を伴います。\n"
    "現在のProject Chatはread-onlyのため実行できません。\n"
    "状況の説明は続けられます。変更が必要な場合はControl Planeから実行してください。"
)


class ProjectChatService:
    """Answer questions about one project from its situation projection."""

    def __init__(
        self,
        runtime,
        *,
        history_limit: int = 12,
        backend_name: str = BACKEND_NAME,
    ) -> None:
        self.runtime = runtime
        self.history_limit = history_limit
        self.backend_name = backend_name

    # --- reads -------------------------------------------------------------

    def history(self, project_id: str) -> dict[str, Any] | None:
        """Return the durable thread of ``project_id``, or ``None`` if unknown."""

        situation = get_project_situation(self.runtime, project_id)
        if situation is None:
            return None
        store = self.runtime.chat_store
        thread = store.get_thread(project_id)
        messages = store.messages(thread.id) if thread is not None else []
        return {
            "project_id": project_id,
            "thread_id": str(thread.id) if thread is not None else None,
            "llm": self._llm_availability(),
            "messages": [item.to_dict() for item in messages],
        }

    # --- one exchange ------------------------------------------------------

    async def ask(
        self, project_id: str, message: str, *, refuse_state_changes: bool = True
    ) -> dict[str, Any] | None:
        """Answer one question, or return ``None`` when the project is unknown.

        The situation is recompiled for every question, so an answer always
        describes the project as it is now rather than as it was when the
        thread started (Invariant 193).

        ``refuse_state_changes=False`` is for the one caller that has already
        decided this message asks for nothing to happen (see
        :mod:`nexus_seed.chat.message`).  Judging it twice, by two different
        rules, could only produce a contradiction.  It does not open a write
        path: this module still has none.
        """

        text = (message or "").strip()
        if not text:
            raise ValueError("message must not be empty")
        situation = get_project_situation(self.runtime, project_id)
        if situation is None:
            return None

        thread = self.runtime.chat_store.ensure_thread(project_id)
        history = self.runtime.chat_store.messages(thread.id, limit=self.history_limit)

        refusal = self._refusal(
            project_id, text, refuse_state_changes=refuse_state_changes
        )
        if refusal is not None:
            answer, status, certainty, metadata = refusal
        else:
            answer, status, certainty, metadata = await self._answer(
                situation, history, text
            )

        question = self.runtime.chat_store.append(
            ProjectChatMessage(
                thread_id=thread.id,
                project_id=project_id,
                role=ChatRole.HUMAN,
                text=text,
            )
        )
        reply = self.runtime.chat_store.append(
            ProjectChatMessage(
                thread_id=thread.id,
                project_id=project_id,
                role=ChatRole.NEXUS_SEED,
                text=answer,
                status=status,
                certainty=certainty,
                references=tuple(metadata.pop("references", ())),
                metadata=metadata,
            )
        )
        return {
            "project_id": project_id,
            "thread_id": str(thread.id),
            "answer": reply.text,
            "status": reply.status.value,
            "certainty": reply.certainty.value if reply.certainty else None,
            "references": list(reply.references),
            "question_message_id": str(question.id),
            "answer_message_id": str(reply.id),
            "situation_updated_at": (
                situation.updated_at.isoformat() if situation.updated_at else None
            ),
            "created_at": reply.created_at.isoformat(),
        }

    # --- guards ------------------------------------------------------------

    def _refusal(self, project_id: str, text: str, *, refuse_state_changes: bool = True):
        """Refuse out-of-scope and state-changing requests before the LLM sees them."""

        foreign = detect_foreign_projects(
            text,
            project_id=project_id,
            known_projects=get_project_summaries(self.runtime),
        )
        if foreign:
            named = "、".join(foreign)
            return (
                f"この会話は {project_id} にscopeされています。\n"
                f"{named} の情報はこのThreadでは扱えません。\n"
                f"{named} のProject Chatを開いて質問してください。",
                ChatAnswerStatus.OUT_OF_SCOPE,
                ChatCertainty.FACT,
                {"out_of_scope_projects": foreign},
            )
        change = detect_state_change_request(text) if refuse_state_changes else None
        if change is not None:
            return (
                READ_ONLY_ANSWER,
                ChatAnswerStatus.READ_ONLY_REFUSED,
                ChatCertainty.FACT,
                {"detected_request": change},
            )
        return None

    # --- the LLM call ------------------------------------------------------

    async def _answer(self, situation, history, text: str):
        """Ask the configured LLM, falling back to projection facts on failure."""

        backend = self.runtime.backends.get(self.backend_name)
        if backend is None:
            return (
                deterministic_answer(situation),
                ChatAnswerStatus.LLM_UNAVAILABLE,
                ChatCertainty.FACT,
                {"reason": f"no {self.backend_name!r} backend is configured"},
            )

        request = BackendRequest(
            instruction=INSTRUCTION,
            context=build_chat_context(situation, history, text),
            output_schema=ANSWER_SCHEMA,
            metadata={"project_id": situation.project_id, "read_only": True},
        )
        result = await backend.execute(request)
        if not result.success:
            return (
                deterministic_answer(situation),
                ChatAnswerStatus.LLM_FAILED,
                ChatCertainty.FACT,
                {"reason": result.error, "model": result.model},
            )

        parsed = _valid_answer(result.parsed_output)
        if parsed is None:
            return (
                deterministic_answer(situation),
                ChatAnswerStatus.LLM_INVALID,
                ChatCertainty.FACT,
                {
                    "reason": "LLM answer did not match the Project Chat schema",
                    "model": result.model,
                },
            )

        answer, certainty, references = parsed
        allowed = known_references(situation)
        return (
            answer,
            ChatAnswerStatus.ANSWERED,
            certainty,
            {
                "model": result.model,
                "references": [item for item in references if item in allowed],
            },
        )

    def _llm_availability(self) -> dict[str, Any]:
        backend = self.runtime.backends.get(self.backend_name)
        return {
            "enabled": backend is not None,
            "model": getattr(backend, "model", None) if backend is not None else None,
        }


def _valid_answer(output: Any):
    """Validate structured LLM output, returning ``None`` when unusable."""

    if not isinstance(output, dict):
        return None
    answer = output.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return None
    raw_certainty = output.get("certainty")
    try:
        certainty = ChatCertainty(str(raw_certainty).upper())
    except ValueError:
        # An unlabelled answer is treated as reasoning, never as a stated fact.
        certainty = ChatCertainty.INFERENCE
    references = output.get("references")
    if not isinstance(references, list):
        references = []
    return (
        answer.strip(),
        certainty,
        [str(item) for item in references if isinstance(item, (str, int))],
    )


__all__ = [
    "ANSWER_SCHEMA",
    "BACKEND_NAME",
    "INSTRUCTION",
    "READ_ONLY_ANSWER",
    "ProjectChatService",
]
