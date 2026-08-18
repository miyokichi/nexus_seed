"""AgentManager — one Agent per Project: assign, spawn, stop, health.

The rule is deliberately blunt: **one Project = one Agent**.  NEXUS SEED never
picks an executor per unit of work; it picks one Agent when the Project starts
and delegates the whole Goal to it.  If a Project already has a live Agent, that
Agent is reused.
"""

from __future__ import annotations

import logging
from typing import Any

from ..storage.orchestrator_store import AgentStore
from .agent_runtime import AgentRuntime
from .models import Agent, AgentStatus, Project, ProjectAgentConfig, new_agent_id

logger = logging.getLogger("nexus_seed.orchestrator.agent_manager")


class AgentManager:
    """Owns the Agent side of the one-Project-one-Agent rule."""

    def __init__(
        self,
        store: AgentStore,
        runtime: AgentRuntime,
        *,
        default_constraints: dict[str, Any] | None = None,
        workspace_root: str | None = None,
        available_skills: tuple[str, ...] = (),
        a2a_endpoint: str | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.default_constraints = dict(default_constraints or {})
        self.workspace_root = workspace_root
        self.available_skills = tuple(available_skills)
        self.a2a_endpoint = a2a_endpoint

    async def assign_or_spawn(self, project: Project) -> Agent:
        """Return the Agent that owns ``project``, starting one if needed."""
        existing = self.store.active_for_project(project.id)
        if existing is not None:
            logger.info("project %s reuses agent %s", project.id, existing.agent_id)
            return existing
        return await self.spawn(project)

    async def spawn(self, project: Project) -> Agent:
        """Start a new generic Project Agent for ``project``."""
        agent = Agent(
            project_id=project.id,
            runtime=self.runtime.name,
            status=AgentStatus.STARTING,
            agent_id=new_agent_id(),
        )
        self.store.save(agent)

        config = self.build_config(project, agent)
        try:
            endpoint = await self.runtime.spawn(config)
        except Exception as exc:  # noqa: BLE001 - surface as a failed agent
            agent.status = AgentStatus.FAILED
            agent.metadata = {**agent.metadata, "error": str(exc)}
            self.store.save(agent)
            logger.exception("failed to start agent for project %s", project.id)
            raise

        agent.endpoint = endpoint or None
        agent.status = AgentStatus.RUNNING
        self.store.save(agent)
        logger.info("project %s assigned agent %s", project.id, agent.agent_id)
        return agent

    def build_config(self, project: Project, agent: Agent) -> ProjectAgentConfig:
        """Build the config a generic Project Agent is started with."""
        workspace = None
        if self.workspace_root is not None:
            workspace = f"{self.workspace_root.rstrip('/')}/{project.id}"
        return ProjectAgentConfig(
            agent_id=agent.agent_id,
            project_id=project.id,
            goal=project.goal,
            project_context=dict(project.context),
            constraints=dict(self.default_constraints),
            workspace=workspace,
            available_skills=self.available_skills,
            nexus_seed_a2a_endpoint=self.a2a_endpoint,
        )

    def get(self, agent_id: str) -> Agent | None:
        """Return an agent by id."""
        return self.store.get(agent_id)

    def for_project(self, project_id: str) -> Agent | None:
        """Return the live agent owning ``project_id``, if any."""
        return self.store.active_for_project(project_id)

    def set_status(self, agent: Agent, status: AgentStatus) -> Agent:
        """Record a new agent status."""
        agent.status = status
        return self.store.save(agent)

    async def idle(self, agent: Agent) -> Agent:
        """Park an agent that has finished its project but stays available."""
        return self.set_status(agent, AgentStatus.IDLE)

    async def stop(self, agent: Agent) -> Agent:
        """Stop an agent and record it as stopped."""
        try:
            await self.runtime.stop(agent.agent_id)
        except Exception:  # noqa: BLE001 - stopping must not break orchestration
            logger.exception("agent %s did not stop cleanly", agent.agent_id)
        return self.set_status(agent, AgentStatus.STOPPED)

    async def health(self, agent: Agent) -> bool:
        """Ask the runtime whether ``agent`` is still usable."""
        try:
            return await self.runtime.health(agent.agent_id)
        except Exception:  # noqa: BLE001
            logger.exception("health check failed for agent %s", agent.agent_id)
            return False


__all__ = ["AgentManager"]
