"""A2AGateway — the only channel between NEXUS SEED and its Project Agents.

Transport plus audit, and nothing else.  It sends goals and added tasks out to
an Agent, collects whatever the Agents send back, and records both directions.
Deciding what an escalation *means* belongs to the orchestrator, not here.
"""

from __future__ import annotations

import logging
from typing import Any

from ..storage.orchestrator_store import A2AMessageStore
from .agent_runtime import AgentRuntime
from .models import A2AMessage, Agent, Project

logger = logging.getLogger("nexus_seed.orchestrator.a2a_gateway")


class A2AGateway:
    """Carries messages between NEXUS SEED and Project Agents."""

    def __init__(self, store: A2AMessageStore, runtime: AgentRuntime) -> None:
        self.store = store
        self.runtime = runtime

    async def assign_goal(self, project: Project, agent: Agent) -> dict[str, Any]:
        """Delegate a whole Goal to its Agent."""
        envelope = {
            "kind": "ASSIGN_GOAL",
            "project_id": project.id,
            "goal": project.goal,
            "context": dict(project.context),
            "priority": project.priority,
        }
        await self.runtime.deliver(agent.agent_id, envelope)
        logger.info("project %s goal delegated to agent %s", project.id, agent.agent_id)
        return envelope

    async def add_task(
        self, project: Project, agent: Agent, task: dict[str, Any]
    ) -> dict[str, Any]:
        """Send an additional Task for a Goal the Agent already owns."""
        envelope = {
            "kind": "ADD_TASK",
            "project_id": project.id,
            "task": task,
        }
        await self.runtime.deliver(agent.agent_id, envelope)
        logger.info("project %s task %s sent to agent %s", project.id, task.get("id"), agent.agent_id)
        return envelope

    async def poll(self) -> list[A2AMessage]:
        """Collect and record everything the Agents have sent back."""
        messages = await self.runtime.poll()
        for message in messages:
            self.store.append(message, direction="inbound")
        return messages

    def record_outbound(self, message: A2AMessage) -> A2AMessage:
        """Record a message NEXUS SEED sent to an Agent."""
        return self.store.append(message, direction="outbound")

    def history(self, project_id: str) -> list[tuple[str, A2AMessage]]:
        """Return the recorded channel history for one project."""
        return self.store.for_project(project_id)


__all__ = ["A2AGateway"]
