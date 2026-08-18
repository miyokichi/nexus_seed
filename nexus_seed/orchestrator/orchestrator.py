"""ProjectOrchestrator — NEXUS SEED as a Project Orchestrator.

The whole system in one sentence: *look at the context, start a Project, hand it
to an Agent, and deal with what comes back.*

    ContextManager -> ProjectRouter -> ProjectManager -> AgentManager -> A2AGateway

NEXUS SEED does not decompose the Goal, choose a capability per unit of work, or
run tools.  The Project Agent does all of that.  NEXUS SEED owns which projects
exist, how urgent they are, who holds them, what they are waiting on, and when
they end — and it is the only thing allowed to create a Project.
"""

from __future__ import annotations

import logging
from typing import Any

from ..backends.base import ExecutionBackend
from ..storage.database import Database
from ..storage.orchestrator_store import A2AMessageStore, AgentStore, ProjectStore
from .a2a_gateway import A2AGateway
from .agent_manager import AgentManager
from .agent_runtime import AgentRuntime, AgentUnavailable, InProcessAgentRuntime
from .context_manager import ContextManager
from .models import (
    BLOCKING_ESCALATIONS,
    A2AMessage,
    A2AMessageType,
    Agent,
    AgentStatus,
    Project,
    ProjectStatus,
    RoutingAction,
    RoutingDecision,
)
from .project_manager import ProjectManager
from .router import ProjectRouter

logger = logging.getLogger("nexus_seed.orchestrator")

#: Safety valve so a misbehaving agent cannot spin the drain loop forever.
MAX_DRAIN_ROUNDS = 100


class ProjectOrchestrator:
    """Creates Projects, delegates them to Agents, and handles escalations."""

    def __init__(
        self,
        db_path: str | Database,
        *,
        agent_runtime: AgentRuntime | None = None,
        backend: ExecutionBackend | None = None,
        world_state_provider: Any | None = None,
        user_context_provider: Any | None = None,
        workspace_root: str | None = None,
        available_skills: tuple[str, ...] = (),
        a2a_endpoint: str | None = None,
        default_constraints: dict[str, Any] | None = None,
    ) -> None:
        self.db = db_path if isinstance(db_path, Database) else Database(db_path)
        self.agent_runtime = agent_runtime or InProcessAgentRuntime()

        self.project_store = ProjectStore(self.db)
        self.agent_store = AgentStore(self.db)
        self.message_store = A2AMessageStore(self.db)

        self.projects = ProjectManager(self.project_store)
        self.context = ContextManager(
            self.projects,
            world_state_provider=world_state_provider,
            user_context_provider=user_context_provider,
        )
        self.router = ProjectRouter(backend)
        self.agents = AgentManager(
            self.agent_store,
            self.agent_runtime,
            default_constraints=default_constraints,
            workspace_root=workspace_root,
            available_skills=available_skills,
            a2a_endpoint=a2a_endpoint,
        )
        self.gateway = A2AGateway(self.message_store, self.agent_runtime)

    # --- incoming requests -------------------------------------------------

    async def handle_request(
        self,
        request: str,
        *,
        source: str = "user",
        user_context: dict[str, Any] | None = None,
        origin_project_id: str | None = None,
        priority: int = 0,
    ) -> RoutingDecision:
        """Route one request, act on the decision, then settle the system.

        Returns the decision that was acted on, so a caller can see whether a
        project was created, extended, or deliberately ignored.
        """
        decision, _project = await self.submit(
            request,
            source=source,
            user_context=user_context,
            origin_project_id=origin_project_id,
            priority=priority,
        )
        return decision

    async def submit(
        self,
        request: str,
        *,
        source: str = "user",
        user_context: dict[str, Any] | None = None,
        origin_project_id: str | None = None,
        priority: int = 0,
    ) -> tuple[RoutingDecision, Project | None]:
        """Handle one request and return the decision *and* the Project it touched.

        The Project is re-read after the Agents have been drained, so what
        comes back is the settled state rather than the state at delegation.
        ``None`` means the request deliberately produced no project work.
        """
        context = self.context.build(
            request,
            source=source,
            user_context=user_context,
            origin_project_id=origin_project_id,
        )
        decision = await self.router.route(context)
        logger.info(
            "routing %r -> %s (%s)", request[:60], decision.action.value, decision.reason
        )
        touched = await self.apply(
            decision, priority=priority, origin_project_id=origin_project_id
        )
        await self.drain()
        return decision, (self.projects.get(touched.id) if touched else None)

    async def apply(
        self,
        decision: RoutingDecision,
        *,
        priority: int = 0,
        origin_project_id: str | None = None,
    ) -> Project | None:
        """Carry out a routing decision.  Returns the project it touched."""
        if decision.action is RoutingAction.IGNORE:
            return None

        if decision.action is RoutingAction.CREATE_PROJECT:
            return await self.start_project(
                decision.proposed_goal or "",
                priority=priority,
                parent_project_id=origin_project_id,
                context={"routing_reason": decision.reason},
            )

        project = self.projects.get(decision.target_project_id or "")
        if project is None:
            logger.warning("decision named missing project %s", decision.target_project_id)
            return None

        if decision.action is RoutingAction.ADD_TASK_TO_PROJECT:
            task = self.projects.add_task(project, decision.proposed_task or "")
            agent = await self.assign_agent(project)
            if agent is None:
                return project
            await self.delegate(project, agent, task=task)
            return project

        # UPDATE_PROJECT: management-level framing only.
        if decision.reason:
            self.projects.set_summary(project, decision.reason)
        if priority:
            self.projects.set_priority(project, priority)
        return project

    async def start_project(
        self,
        goal: str,
        *,
        priority: int = 0,
        parent_project_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Project:
        """Create a Project, give it an Agent, and delegate the whole Goal."""
        project = self.projects.create(
            goal,
            context=context,
            priority=priority,
            parent_project_id=parent_project_id,
        )
        agent = await self.assign_agent(project)
        if agent is None:
            return project
        if await self.delegate(project, agent):
            self.projects.activate(project)
        return project

    # --- delegation ---------------------------------------------------------

    async def assign_agent(self, project: Project) -> Agent | None:
        """Give ``project`` its Agent, or ``None`` when no runtime answered.

        An Agent Runtime that is not there is a transport problem, so the
        Project keeps its status and simply has no Agent yet: the next attempt
        assigns one.  It is never turned into a blocker on the Project.
        """
        try:
            agent = await self.agents.assign_or_spawn(project)
        except AgentUnavailable as exc:
            logger.warning("project %s has no agent yet: %s", project.id, exc)
            return None
        self.projects.assign_agent(project, agent.agent_id)
        return agent

    async def delegate(
        self, project: Project, agent: Agent, *, task: dict[str, Any] | None = None
    ) -> bool:
        """Hand the Goal (or one added Task) over.  ``False`` if unreachable.

        Same rule as assignment: an unreachable Agent is recorded on the Agent
        and retried later, never written onto the Project as a blocker.
        """
        try:
            if task is None:
                await self.gateway.assign_goal(project, agent)
            else:
                await self.gateway.add_task(project, agent, task)
        except AgentUnavailable as exc:
            self.agents.record_unavailable(agent, str(exc))
            return False
        self.agents.clear_unavailable(agent)
        return True

    # --- messages from agents ---------------------------------------------

    async def drain(self) -> list[A2AMessage]:
        """Handle everything the Agents have sent until they go quiet."""
        handled: list[A2AMessage] = []
        for _ in range(MAX_DRAIN_ROUNDS):
            messages = await self.gateway.poll()
            if not messages:
                break
            for message in messages:
                await self.handle_message(message)
                handled.append(message)
        else:  # pragma: no cover - defensive
            logger.error("drain did not settle after %d rounds", MAX_DRAIN_ROUNDS)
        return handled

    async def handle_message(self, message: A2AMessage) -> None:
        """Decide what one Agent message means for its Project."""
        if message.type is A2AMessageType.DISCOVERED_NEW_PROJECT:
            await self._handle_discovery(message)
            return

        project = self.projects.get(message.project_id or "")
        if project is None:
            logger.warning(
                "message %s names unknown project %s", message.type.value, message.project_id
            )
            return

        if message.type is A2AMessageType.PROJECT_STATUS:
            summary = str(message.payload.get("summary", ""))
            if summary:
                self.projects.set_summary(project, summary)
            if project.status is ProjectStatus.CREATED:
                self.projects.activate(project)
            return

        if message.type is A2AMessageType.PROJECT_COMPLETED:
            await self._handle_completion(project, message)
            return

        if message.type is A2AMessageType.NEED_HUMAN_INPUT:
            self.projects.wait_for_human(
                project,
                reason=str(message.payload.get("reason", "agent asked for a person")),
                detail=dict(message.payload),
            )
            return

        if message.type in BLOCKING_ESCALATIONS:
            self.projects.block(
                project,
                kind=message.type.value,
                reason=str(message.payload.get("reason", message.type.value)),
                detail=dict(message.payload),
            )
            return

        logger.warning("unhandled A2A message type %s", message.type)  # pragma: no cover

    async def _handle_completion(self, project: Project, message: A2AMessage) -> None:
        """Complete a project and release its agent."""
        self.projects.complete(project, summary=str(message.payload.get("summary", "")))
        agent = self.agents.for_project(project.id)
        if agent is not None:
            await self.agents.idle(agent)

    async def _handle_discovery(self, message: A2AMessage) -> None:
        """An Agent found an independent problem: route it like any request.

        The Agent does not get to create the Project — it reports, and NEXUS
        SEED decides.  That keeps project creation in one place.
        """
        described = str(
            message.payload.get("goal") or message.payload.get("description") or ""
        ).strip()
        if not described:
            logger.warning("discovery from %s carried no goal", message.source_agent_id)
            return

        parent_id = message.project_id
        context = self.context.build(
            described,
            source="agent_discovery",
            user_context={"discovered_by": message.source_agent_id},
            origin_project_id=parent_id,
        )
        decision = await self.router.route(context)
        logger.info(
            "discovery from project %s -> %s", parent_id, decision.action.value
        )
        await self.apply(
            decision,
            priority=int(message.payload.get("priority", 0) or 0),
            origin_project_id=parent_id,
        )

    # --- human resolution --------------------------------------------------

    async def resolve_block(
        self, project_id: str, *, note: str = "", reactivate: bool = True
    ) -> Project | None:
        """Clear a project's blockers once a person or a Skill supplied what was missing."""
        project = self.projects.get(project_id)
        if project is None:
            return None
        self.projects.clear_blockers(project)
        if note:
            self.projects.set_summary(project, note)
        if reactivate:
            self.projects.activate(project)
            agent = await self.assign_agent(project)
            if agent is not None:
                if agent.status is AgentStatus.IDLE:
                    self.agents.set_status(agent, AgentStatus.RUNNING)
                await self.delegate(project, agent)
                await self.drain()
        return self.projects.get(project_id)

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the database."""
        self.db.close()

    def __enter__(self) -> "ProjectOrchestrator":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["MAX_DRAIN_ROUNDS", "ProjectOrchestrator"]
