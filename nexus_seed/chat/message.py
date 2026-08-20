"""One box per project: NEXUS SEED decides whether you asked or told it.

Two boxes made the person do the classifying — "is this a question or an
instruction?" — which is exactly the judgement NEXUS SEED already makes.  The
ProjectRouter reads a message for meaning and answers with an action, and
``IGNORE`` already means "this needs no project work at all".  That is a
question::

    message + project_id
        -> ProjectRouter
             IGNORE -> Project Chat explains it, read-only
             else   -> the Project Orchestrator acts, and the Agent is told

Both outcomes are appended to the same durable thread, so the project's history
reads as one conversation rather than two half-conversations.

With no reasoning backend nothing can judge meaning, so the deterministic
change-request guard decides instead: a message that plainly asks for something
to happen is acted on, and everything else is explained.  Erring towards
explaining is the safe direction — an answer can be ignored, delegated work
cannot be un-delegated.

Either judge will sometimes be wrong, so ``act=True`` lets the person overrule
it: this message is an instruction, hand it over.  That is the whole of the
correction — one bit, not a second box and not a verb to learn.
"""

from __future__ import annotations

from typing import Any

from ..orchestrator.models import RoutingAction, RoutingDecision
from ..storage.orchestrator_store import InstructionLedger
from .guards import detect_state_change_request
from .models import ChatAnswerStatus, ChatCertainty, ChatRole, ProjectChatMessage
from .service import ProjectChatService


#: What the caller is told this message turned out to be.
ASKED = "ASKED"
TOLD = "TOLD"


class ProjectMessageService:
    """Route one project message to an answer or to the Project Orchestrator."""

    def __init__(self, runtime, chat: ProjectChatService | None = None) -> None:
        self.runtime = runtime
        self.chat = chat if chat is not None else ProjectChatService(runtime)

    async def send(
        self,
        project_id: str,
        message: str,
        *,
        request_id: str | None = None,
        act: bool = False,
    ) -> dict[str, Any] | None:
        """Handle one message, or return ``None`` when the project is unknown.

        ``act=True`` says the person has already decided this is an
        instruction — usually because the message was explained when they
        meant it to be carried out.  Their judgement wins: overruling it would
        leave them with no way to act at all.
        """

        text = (message or "").strip()
        if not text:
            raise ValueError("message must not be empty")

        orchestrator = getattr(self.runtime, "project_orchestrator", None)
        project = (
            orchestrator.projects.get(project_id) if orchestrator is not None else None
        )
        if project is None:
            # Not an orchestrator Project: there is nothing that could act on
            # it, so the message can only be a question.
            return self._as_answer(await self.chat.ask(project_id, text))

        if act:
            return await self._act(orchestrator, project_id, text, request_id)

        if not self._could_act(orchestrator, text):
            return self._as_answer(
                await self.chat.ask(project_id, text, refuse_state_changes=False)
            )

        decision, touched = await orchestrator.submit(
            text,
            source="project_message",
            origin_project_id=project_id,
            request_id=request_id,
        )
        if decision.action is RoutingAction.IGNORE:
            answer = await self.chat.ask(project_id, text, refuse_state_changes=False)
            return self._as_answer(answer, decision=decision)
        return self._record_action(project_id, text, decision, touched)

    async def _act(self, orchestrator, project_id, text, request_id):
        """Carry out a message the person has declared to be an instruction.

        The router is not asked: it has already read this message and been
        overruled, so asking again would only reproduce the same answer.  The
        instruction becomes a Task on the project it was sent from.
        """

        key = InstructionLedger.key("project_message.act", project_id, request_id or "")
        replay = orchestrator.instruction_ledger.get(key) if request_id else None
        if replay is not None:
            decision = RoutingDecision(
                action=RoutingAction.ADD_TASK_TO_PROJECT,
                target_project_id=project_id,
                proposed_task=text,
                reason=str(replay.get("decision", {}).get("reason") or ""),
            )
            return self._record_action(
                project_id, text, decision, orchestrator.projects.get(project_id)
            )

        decision = RoutingDecision(
            action=RoutingAction.ADD_TASK_TO_PROJECT,
            target_project_id=project_id,
            proposed_task=text,
            reason="the person sent this as an instruction",
        )
        touched = await orchestrator.apply(decision, origin_project_id=project_id)
        await orchestrator.drain()
        settled = orchestrator.projects.get(touched.id) if touched else None
        if request_id:
            orchestrator.instruction_ledger.record(
                key,
                origin_project_id=project_id,
                request_id=request_id,
                source="project_message.act",
                message=text,
                decision=decision.to_dict(),
                affected_project_id=settled.id if settled else None,
            )
        return self._record_action(project_id, text, decision, settled)

    # --- deciding ----------------------------------------------------------

    @staticmethod
    def _could_act(orchestrator, text: str) -> bool:
        """Whether this message should reach the router at all.

        With a reasoning backend the router judges everything, because it reads
        for meaning.  Without one, only a message that plainly asks for
        something to happen is acted on — the router's own fallback would
        otherwise turn a question into work.
        """

        if orchestrator.router.backend is not None:
            return True
        return detect_state_change_request(text) is not None

    # --- writing the thread ------------------------------------------------

    def _as_answer(self, answer, *, decision=None) -> dict[str, Any] | None:
        if answer is None:
            return None
        result = {**answer, "kind": ASKED}
        if decision is not None:
            result["routing"] = decision.to_dict()
        return result

    def _record_action(self, project_id, text, decision, touched) -> dict[str, Any]:
        """Append the instruction and what it did to the project's own thread."""

        thread = self.runtime.chat_store.ensure_thread(project_id)
        summary = self._summary(decision, touched)
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
                text=summary,
                status=ChatAnswerStatus.INSTRUCTION_EXECUTED,
                certainty=ChatCertainty.FACT,
                metadata={
                    "kind": "INSTRUCTION",
                    "routing": decision.to_dict(),
                    "affected_project_id": touched.id if touched else None,
                },
            )
        )
        return {
            "kind": TOLD,
            "project_id": project_id,
            "thread_id": str(thread.id),
            "answer": reply.text,
            "status": reply.status.value,
            "routing": decision.to_dict(),
            "affected_project_id": touched.id if touched else None,
            "question_message_id": str(question.id),
            "answer_message_id": str(reply.id),
            "created_at": reply.created_at.isoformat(),
        }

    @staticmethod
    def _summary(decision, touched) -> str:
        """Say what happened, in the terms the person used."""

        if decision.action is RoutingAction.ADD_TASK_TO_PROJECT:
            task = decision.proposed_task or "指示"
            return f"このProjectのTaskとしてAgentに渡しました。\n{task}"
        if decision.action is RoutingAction.CREATE_PROJECT:
            goal = decision.proposed_goal or "新しい目的"
            where = f"（{touched.id}）" if touched is not None else ""
            return f"独立した目的と判断し、別のProjectを作ってAgentに渡しました{where}。\n{goal}"
        if decision.action is RoutingAction.UPDATE_PROJECT:
            return f"Projectの方針を更新しました。\n{decision.reason}"
        return decision.reason or "処理しました。"


__all__ = ["ASKED", "TOLD", "ProjectMessageService"]
