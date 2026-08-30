"""A2AGateway — the only channel between NEXUS SEED and its Project Agents.

Transport plus audit, and nothing else.  It builds what one hand-over says,
gives it to the Agent Runtime, asks whether an outstanding hand-over has
finished, collects whatever the Agents send back, and records every direction.
Deciding what an escalation *means* belongs to the orchestrator, not here.

Handing over and getting the answer are two steps, because a Project takes as
long as it takes::

    deliver -> Dispatch(handle)      the Agent has the work
    collect(handle) -> messages      the Agent has finished it
"""

from __future__ import annotations

import logging
from typing import Any

from .adapters.sqlite import A2AMessageStore
from .agent_runtime import AgentRuntime, Dispatch
from .models import A2AMessage, Agent, AgentAssignment, Project

logger = logging.getLogger("nexus_seed.orchestrator.a2a_gateway")


class A2AGateway:
    """Carries messages between NEXUS SEED and Project Agents."""

    def __init__(self, store: A2AMessageStore, runtime: AgentRuntime) -> None:
        self.store = store
        self.runtime = runtime

    # --- outbound ----------------------------------------------------------

    @staticmethod
    def envelope(project: Project, assignment: AgentAssignment) -> dict[str, Any]:
        """Build what one hand-over says, from the Project it is about.

        Rebuilt from the Project every time rather than stored: the assignment
        record only says *which* hand-over this is, so a re-send after a
        restart carries the project as it stands now, not as it once was.
        """
        envelope: dict[str, Any] = {
            "kind": assignment.kind,
            "project_id": project.id,
            "goal": project.goal,
            "context": dict(project.context),
            "priority": project.priority,
        }
        blockers = _blockers(project, assignment)
        if blockers:
            envelope["blockers"] = blockers
        if assignment.kind == "ADD_TASK":
            envelope["task"] = project.task(assignment.task_id) or {}
        return envelope

    async def deliver(
        self, project: Project, agent: Agent, envelope: dict[str, Any]
    ) -> Dispatch:
        """Hand a Goal or an added Task to its Agent and record that we did."""
        dispatch = await self.runtime.deliver(agent.agent_id, envelope)
        logger.info(
            "project %s %s sent to agent %s%s",
            project.id,
            envelope.get("kind"),
            agent.agent_id,
            f" as {dispatch.handle}" if dispatch.pending else "",
        )
        return dispatch

    # --- inbound -----------------------------------------------------------

    async def collect(self, agent: Agent, handle: str) -> list[A2AMessage] | None:
        """Ask whether one outstanding hand-over has finished.

        ``None`` means the Agent is still working on it, which is not news and
        is deliberately not recorded.
        """
        messages = await self.runtime.collect(agent.agent_id, handle)
        if messages is None:
            return None
        return self.record(messages)

    async def abandon(self, agent: Agent, handle: str) -> None:
        """Stop waiting on one hand-over, without giving up on the Agent."""
        await self.runtime.abandon(agent.agent_id, handle)

    async def poll(self) -> list[A2AMessage]:
        """Collect and record everything the Agents have pushed."""
        return self.record(await self.runtime.poll())

    def record(self, messages: list[A2AMessage]) -> list[A2AMessage]:
        """Record and return only previously unseen inbound messages."""
        recorded = []
        for message in messages:
            if self.store.append_new(message, direction="inbound"):
                recorded.append(message)
        return recorded

    def record_outbound(self, message: A2AMessage) -> A2AMessage:
        """Record a message NEXUS SEED sent to an Agent."""
        return self.store.append(message, direction="outbound")

    def history(self, project_id: str) -> list[tuple[str, A2AMessage]]:
        """Return the recorded channel history for one project."""
        return self.store.for_project(project_id)


def _blockers(project: Project, assignment: AgentAssignment) -> list[dict[str, Any]]:
    """What the Agent is stuck on, or was stuck on until this hand-over.

    A follow-up instruction has to arrive against something: "use June only"
    means nothing unless the Agent is also told that the SAP data it asked for
    is what it is being told to do without.  A blocker this very task resolved
    is therefore sent too, marked as no longer in the way.
    """
    resolved_here = f"task {assignment.task_id}" if assignment.task_id else None
    blockers = []
    for blocker in project.blockers:
        resolved = bool(blocker.get("resolved_at"))
        if resolved and blocker.get("resolved_by") != resolved_here:
            continue
        blockers.append(
            {
                "kind": blocker.get("kind", ""),
                "reason": blocker.get("reason", ""),
                "resolved": resolved,
            }
        )
    return blockers


__all__ = ["A2AGateway"]
