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

import asyncio
import logging
from datetime import timedelta
from typing import Any

from ..backends.base import ExecutionBackend
from ..storage.database import Database
from ..storage.orchestrator_store import (
    A2AMessageStore,
    AgentStore,
    InstructionLedger,
    ProjectStore,
)
from .a2a_gateway import A2AGateway
from .agent_manager import AgentManager
from .agent_runtime import (
    AgentRuntime,
    AgentUnavailable,
    InProcessAgentRuntime,
    RemoteWorkLost,
)
from .context_manager import ContextManager
from ..core.event import utcnow
from .models import (
    BLOCKING_ESCALATIONS,
    A2AMessage,
    A2AMessageType,
    Agent,
    AgentAssignment,
    AgentStatus,
    AssignmentStatus,
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

#: How many times one hand-over is attempted before NEXUS SEED stops asking.
MAX_DISPATCH_ATTEMPTS = 5

#: First retry gap, doubling per attempt up to :data:`RETRY_MAX_SECONDS`.
RETRY_BASE_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0

#: How long an Agent may hold a hand-over before NEXUS SEED gives up on it.
#: A Project Agent owns a whole Goal, so this is generous by design.
ASSIGNMENT_TIMEOUT_SECONDS = 3600.0


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
        max_dispatch_attempts: int = MAX_DISPATCH_ATTEMPTS,
        retry_base_seconds: float = RETRY_BASE_SECONDS,
        retry_max_seconds: float = RETRY_MAX_SECONDS,
        assignment_timeout_seconds: float = ASSIGNMENT_TIMEOUT_SECONDS,
    ) -> None:
        self.db = db_path if isinstance(db_path, Database) else Database(db_path)
        self.agent_runtime = agent_runtime or InProcessAgentRuntime()
        self.max_dispatch_attempts = max_dispatch_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.assignment_timeout_seconds = assignment_timeout_seconds

        self.project_store = ProjectStore(self.db)
        self.agent_store = AgentStore(self.db)
        self.message_store = A2AMessageStore(self.db)
        self.instruction_ledger = InstructionLedger(self.db)

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
        request_id: str | None = None,
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
            request_id=request_id,
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
        request_id: str | None = None,
    ) -> tuple[RoutingDecision, Project | None]:
        """Handle one request and return the decision *and* the Project it touched.

        The Project is re-read after the Agents have been drained, so what
        comes back is the settled state rather than the state at delegation.
        ``None`` means the request deliberately produced no project work.

        Passing ``request_id`` makes the call exactly-once: a resend of the same
        delivery replays the recorded decision instead of routing again, so a
        double click or a retried HTTP call cannot hand the Agent the same task
        twice.  Without one the call is handled as a fresh request, exactly as
        before.
        """
        replay = self._replay(source, origin_project_id, request_id)
        if replay is not None:
            return replay

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
        settled = self.projects.get(touched.id) if touched else None
        if request_id:
            self.instruction_ledger.record(
                InstructionLedger.key(source, origin_project_id, request_id),
                origin_project_id=origin_project_id,
                request_id=request_id,
                source=source,
                message=request,
                decision=decision.to_dict(),
                affected_project_id=settled.id if settled else None,
            )
        return decision, settled

    def _replay(
        self, source: str, origin_project_id: str | None, request_id: str | None
    ) -> tuple[RoutingDecision, Project | None] | None:
        """Return the recorded outcome of an already-handled delivery, if any."""
        if not request_id:
            return None
        recorded = self.instruction_ledger.get(
            InstructionLedger.key(source, origin_project_id, request_id)
        )
        if recorded is None:
            return None
        logger.info(
            "instruction %s already handled; replaying %s",
            request_id,
            recorded["decision"].get("action"),
        )
        decision = RoutingDecision(
            action=RoutingAction(recorded["decision"]["action"]),
            target_project_id=recorded["decision"].get("target_project_id"),
            proposed_goal=recorded["decision"].get("proposed_goal"),
            proposed_task=recorded["decision"].get("proposed_task"),
            reason=recorded["decision"].get("reason", ""),
            confidence=float(recorded["decision"].get("confidence", 0.0)),
        )
        affected = recorded["affected_project_id"]
        return decision, (self.projects.get(affected) if affected else None)

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
            return await self.add_task(project, decision.proposed_task or "")

        # UPDATE_PROJECT: management-level framing only.
        if decision.reason:
            self.projects.set_summary(project, decision.reason)
        if priority:
            self.projects.set_priority(project, priority)
        return project

    async def add_task(self, project: Project, description: str) -> Project:
        """Give a Project one more Task, and send it to the Agent that owns it.

        This is also how a person unblocks a Project: a follow-up instruction is
        another Task for the same Goal, so whatever was in the way is recorded
        as resolved and the *same* Agent is asked to carry on.
        """
        task = self.projects.add_task(project, description)
        agent = await self.assign_agent(project)
        if agent is None:
            return project
        if project.current_blockers:
            self.projects.resolve_blockers(project, by=f"task {task['id']}")
        if project.status is not ProjectStatus.ACTIVE:
            self.projects.activate(project)
        if agent.status is AgentStatus.IDLE:
            self.agents.set_status(agent, AgentStatus.RUNNING)
        await self.delegate(project, agent, task=task)
        return self.projects.get(project.id) or project

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
        # ACTIVE the moment it has an Agent to work on it: whether the hand-over
        # got through this second is the Agent's state, not the Project's.
        self.projects.activate(project)
        await self.delegate(project, agent)
        return self.projects.get(project.id) or project

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

        This returns once the Agent has *taken* the work, not once it has done
        it: a Project runs for as long as it needs, and what is outstanding is
        recorded on the Agent so :meth:`reconcile` can pick it up later — after
        a restart if need be.

        Same rule as assignment: an unreachable Agent is recorded on the Agent
        and retried later, never written onto the Project as a blocker.
        """
        assignment = AgentAssignment(
            kind="ADD_TASK" if task is not None else "ASSIGN_GOAL",
            task_id=(task or {}).get("id"),
        )
        return await self.hand_over(project, agent, assignment)

    async def hand_over(
        self, project: Project, agent: Agent, assignment: AgentAssignment
    ) -> bool:
        """Send one hand-over and record how it went.  ``False`` if unreachable."""
        assignment.attempts += 1
        envelope = self.gateway.envelope(project, assignment)
        try:
            dispatch = await self.gateway.deliver(project, agent, envelope)
        except AgentUnavailable as exc:
            self._unavailable(agent, assignment, str(exc))
            return False

        assignment.handle = dispatch.handle
        assignment.status = (
            AssignmentStatus.DISPATCHED if dispatch.pending else AssignmentStatus.ANSWERED
        )
        assignment.dispatched_at = utcnow()
        assignment.next_attempt_at = None
        assignment.error = ""
        self.agents.record_assignment(agent, assignment)
        self.agents.clear_unavailable(agent)
        for message in self.gateway.record(dispatch.messages):
            await self.handle_message(message)
        return True

    def _unavailable(
        self, agent: Agent, assignment: AgentAssignment, reason: str
    ) -> None:
        """Record a hand-over that did not get through, and when to try again.

        Bounded on purpose: NEXUS SEED retries a few times with a widening gap
        and then stops asking.  Never a busy loop, and never a Project failure —
        the Project keeps its status and a person can send it on its way again.
        """
        exhausted = assignment.attempts >= self.max_dispatch_attempts
        assignment.status = (
            AssignmentStatus.UNAVAILABLE if exhausted else AssignmentStatus.PENDING
        )
        assignment.error = reason
        assignment.next_attempt_at = (
            None if exhausted else utcnow() + timedelta(seconds=self._backoff(assignment))
        )
        self.agents.record_assignment(agent, assignment)
        self.agents.record_unavailable(agent, reason)
        logger.warning(
            "project %s hand-over attempt %d failed (%s): %s",
            agent.project_id,
            assignment.attempts,
            "giving up for now" if exhausted else f"retry at {assignment.next_attempt_at}",
            reason,
        )

    def _backoff(self, assignment: AgentAssignment) -> float:
        """Seconds to wait before the next attempt, widening but capped."""
        return min(
            self.retry_base_seconds * (2 ** (assignment.attempts - 1)),
            self.retry_max_seconds,
        )

    # --- converging with the Agents ----------------------------------------

    async def reconcile(self) -> list[A2AMessage]:
        """Bring every live Project back in step with its Agent.

        The one loop that moves Projects forward outside a request: it re-adopts
        Agents this process has never seen, asks whether outstanding hand-overs
        have finished, retries the ones that never got through, and hands over
        again when an Agent has forgotten the work.  It is idempotent and reads
        everything from SQLite, so calling it after a restart *is* recovery —
        there is no second reconciliation path to keep in step with this one.
        """
        handled: list[A2AMessage] = []
        for project in self.projects.live():
            agent = self.agents.for_project(project.id)
            if agent is None:
                continue
            assignment = agent.assignment
            if assignment is None or not assignment.is_open:
                continue
            handled.extend(await self._advance(project, agent, assignment))
        handled.extend(await self.drain())
        return handled

    async def _advance(
        self, project: Project, agent: Agent, assignment: AgentAssignment
    ) -> list[A2AMessage]:
        """Move one Project's outstanding hand-over along by one step."""
        try:
            await self.agents.reattach(project, agent)
        except AgentUnavailable as exc:
            logger.warning("cannot reach the runtime holding %s: %s", agent.agent_id, exc)
            return []

        if assignment.status is AssignmentStatus.DISPATCHED:
            return await self._answer(project, agent, assignment)
        # UNAVAILABLE means the retry budget is spent, so reconcile stops asking.
        # The Project keeps its Agent and its status, and the next thing a person
        # sends it starts a fresh hand-over.
        if assignment.status is AssignmentStatus.PENDING and assignment.due(utcnow()):
            await self.hand_over(project, agent, assignment)
        return []

    async def _answer(
        self, project: Project, agent: Agent, assignment: AgentAssignment
    ) -> list[A2AMessage]:
        """Ask whether a dispatched hand-over has finished, and act on the answer."""
        try:
            messages = await self.gateway.collect(agent, assignment.handle)
        except RemoteWorkLost as exc:
            # The Agent answered, and its answer was that the work is gone.
            # NEXUS SEED holds the durable Project, so it can safely be handed
            # over again — and only here, where nothing can still be running.
            logger.info("project %s: %s; handing it over again", project.id, exc)
            assignment.status = AssignmentStatus.PENDING
            assignment.handle = ""
            self.agents.record_assignment(agent, assignment)
            await self.hand_over(project, agent, assignment)
            return []
        except AgentUnavailable as exc:
            self._unavailable(agent, assignment, str(exc))
            return []

        if messages is None:
            if self._overdue(assignment):
                await self.gateway.abandon(agent, assignment.handle)
                self._unavailable(
                    agent,
                    assignment,
                    f"agent did not answer within {self.assignment_timeout_seconds}s",
                )
            return []

        assignment.status = AssignmentStatus.ANSWERED
        self.agents.record_assignment(agent, assignment)
        self.agents.clear_unavailable(agent)
        for message in messages:
            await self.handle_message(message)
        return list(messages)

    def _overdue(self, assignment: AgentAssignment) -> bool:
        """Whether a dispatched hand-over has been outstanding for too long."""
        if assignment.dispatched_at is None:
            return False
        return (
            utcnow() - assignment.dispatched_at
        ).total_seconds() >= self.assignment_timeout_seconds

    async def settle(
        self, project_id: str, *, timeout: float = 1800.0, interval: float = 2.0
    ) -> Project | None:
        """Reconcile until ``project_id`` stops being worked on, or time runs out.

        For a caller that wants to watch one Project through — a command line, a
        test.  A resident NEXUS SEED never needs this: its tick reconciles.
        """
        deadline = utcnow() + timedelta(seconds=timeout)
        while True:
            project = self.projects.get(project_id)
            if project is None or not self.is_working(project):
                return project
            if utcnow() >= deadline:
                logger.info("stopped waiting on project %s", project_id)
                return project
            await asyncio.sleep(interval)
            await self.reconcile()

    def is_working(self, project: Project) -> bool:
        """Whether this Project has a hand-over an Agent has not answered yet."""
        if not project.is_live:
            return False
        agent = self.agents.for_project(project.id)
        assignment = agent.assignment if agent is not None else None
        return assignment is not None and assignment.status in (
            AssignmentStatus.PENDING,
            AssignmentStatus.DISPATCHED,
        )

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


__all__ = [
    "ASSIGNMENT_TIMEOUT_SECONDS",
    "MAX_DISPATCH_ATTEMPTS",
    "MAX_DRAIN_ROUNDS",
    "RETRY_BASE_SECONDS",
    "RETRY_MAX_SECONDS",
    "ProjectOrchestrator",
]
