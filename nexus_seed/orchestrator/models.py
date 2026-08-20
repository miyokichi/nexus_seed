"""Domain records for the Project Orchestrator.

NEXUS SEED orchestrates *projects*; it does not execute their work.  These are
ordinary domain records — none of them is a Core primitive, and none of them
replaces `Event`, `Process`, `State`, `Context`, `Continuation` or `Runtime`.

    Project  = one Goal plus the Work that Goal needs (one Task is already one)
    Agent    = the one Project Agent that owns a Project's execution
    A2A      = the only channel an Agent uses to come back to NEXUS SEED
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow

#: How much of a Project's own words the ProjectRouter is given.
#:
#: The router decides *which* project a request belongs to, so it needs enough
#: to recognise one — not the Agent's whole report.  An unbounded summary makes
#: the routing prompt grow with every project until the decision times out, and
#: a router that cannot answer is a request routed by its fallback.
ROUTING_TEXT_LIMIT = 300

#: How many open tasks and blockers of one Project the router is shown.
ROUTING_LIST_LIMIT = 5

#: Prefix of an orchestrator-owned project identifier.
PROJECT_ID_PREFIX = "project-"

#: Prefix of an orchestrator-owned agent identifier.
AGENT_ID_PREFIX = "agent-"


class ProjectStatus(str, Enum):
    """Lifecycle of a Project, owned by NEXUS SEED (not by its Agent)."""

    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    WAITING_HUMAN = "WAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: Statuses in which a Project still needs its Agent.
LIVE_STATUSES = frozenset(
    {
        ProjectStatus.CREATED,
        ProjectStatus.ACTIVE,
        ProjectStatus.BLOCKED,
        ProjectStatus.WAITING_HUMAN,
    }
)

#: Statuses that end a Project.
TERMINAL_STATUSES = frozenset(
    {ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.CANCELLED}
)


def clip(text: str, limit: int = ROUTING_TEXT_LIMIT) -> str:
    """Shorten ``text`` for a routing prompt, saying that it was shortened."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def new_project_id() -> str:
    """Return a fresh orchestrator project identifier."""
    return f"{PROJECT_ID_PREFIX}{uuid.uuid4()}"


def new_agent_id() -> str:
    """Return a fresh orchestrator agent identifier."""
    return f"{AGENT_ID_PREFIX}{uuid.uuid4()}"


@dataclass
class Project:
    """One Goal, the Tasks it needs, and the Agent that owns its execution.

    NEXUS SEED keeps only what it must to steer: the goal, the status, the
    priority, who is doing it, what it is waiting on, and how it relates to
    other projects.  The internal task breakdown belongs to the Agent.
    """

    goal: str
    context: dict[str, Any] = field(default_factory=dict)
    status: ProjectStatus = ProjectStatus.CREATED
    priority: int = 0
    assigned_agent_id: str | None = None
    parent_project_id: str | None = None
    summary: str = ""
    blockers: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=new_project_id)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def is_live(self) -> bool:
        """Whether this project still needs an Agent."""
        return self.status in LIVE_STATUSES

    @property
    def current_blockers(self) -> list[dict[str, Any]]:
        """What is in the way *now*.

        A blocker is never deleted — once something has stopped a Project, that
        it happened is part of the Project's history.  Resolving one records
        when and by what, and leaves it in :attr:`blockers`.
        """
        return [blocker for blocker in self.blockers if not blocker.get("resolved_at")]

    def task(self, task_id: str | None) -> dict[str, Any] | None:
        """Return the Task with ``task_id``, or ``None``."""
        if not task_id:
            return None
        return next((task for task in self.tasks if task.get("id") == task_id), None)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of this project."""
        return {
            "id": self.id,
            "goal": self.goal,
            "context": self.context,
            "status": self.status.value,
            "priority": self.priority,
            "assigned_agent_id": self.assigned_agent_id,
            "parent_project_id": self.parent_project_id,
            "summary": self.summary,
            "blockers": list(self.blockers),
            "current_blockers": self.current_blockers,
            "tasks": list(self.tasks),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def to_routing_dict(self) -> dict[str, Any]:
        """Return the compact form the ProjectRouter is asked to reason over.

        Compressed on purpose: what a project is about, how it is going, and
        what it is stuck on — clipped, and only the most recent few of each.
        The whole story stays in the record for people to read.
        """
        return {
            "id": self.id,
            "goal": clip(self.goal),
            "status": self.status.value,
            "priority": self.priority,
            "summary": clip(self.summary),
            "open_tasks": [
                clip(task.get("description", ""))
                for task in self.tasks[-ROUTING_LIST_LIMIT:]
            ],
            "blockers": [
                clip(blocker.get("reason", ""))
                for blocker in self.current_blockers[-ROUTING_LIST_LIMIT:]
            ],
        }


class AgentStatus(str, Enum):
    """Lifecycle of a Project Agent as NEXUS SEED sees it."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    IDLE = "IDLE"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class AssignmentStatus(str, Enum):
    """How far one hand-over to an Agent has got."""

    #: Decided, not yet accepted by the Agent Runtime.
    PENDING = "PENDING"
    #: The Agent has the work; NEXUS SEED is waiting for the answer.
    DISPATCHED = "DISPATCHED"
    #: The Agent answered; nothing is outstanding.
    ANSWERED = "ANSWERED"
    #: Could not be handed over, and the retry budget is spent.  This says
    #: nothing about whether the Goal can be reached.
    UNAVAILABLE = "UNAVAILABLE"


#: Assignment states that still need NEXUS SEED to do something about them.
OPEN_ASSIGNMENTS = frozenset(
    {AssignmentStatus.PENDING, AssignmentStatus.DISPATCHED, AssignmentStatus.UNAVAILABLE}
)


@dataclass
class AgentAssignment:
    """What an Agent currently owes NEXUS SEED, and how the hand-over went.

    Kept on the Agent record rather than in a table of its own: an Agent owns
    exactly one Project and has at most one outstanding hand-over, so this *is*
    the Agent's current state.  It carries no copy of the Project — ``kind``
    and ``task_id`` are enough to rebuild what was sent from the Project itself.

    ``handle`` is what the Agent Runtime gave back to ask about later.  Storing
    it is what lets a Project being worked on outlive the NEXUS SEED process.
    """

    kind: str = "ASSIGN_GOAL"
    task_id: str | None = None
    status: AssignmentStatus = AssignmentStatus.PENDING
    handle: str = ""
    attempts: int = 0
    dispatched_at: datetime | None = None
    next_attempt_at: datetime | None = None
    error: str = ""

    @property
    def is_open(self) -> bool:
        """Whether NEXUS SEED still has something to do about this hand-over."""
        return self.status in OPEN_ASSIGNMENTS

    def due(self, now: datetime) -> bool:
        """Whether a retry of this hand-over may be attempted at ``now``."""
        return self.next_attempt_at is None or now >= self.next_attempt_at

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form stored on the Agent record."""
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "status": self.status.value,
            "handle": self.handle,
            "attempts": self.attempts,
            "dispatched_at": _iso(self.dispatched_at),
            "next_attempt_at": _iso(self.next_attempt_at),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AgentAssignment | None":
        """Rebuild an assignment from an Agent's metadata, or ``None``."""
        if not isinstance(data, dict) or not data:
            return None
        try:
            status = AssignmentStatus(data.get("status"))
        except ValueError:
            return None
        return cls(
            kind=str(data.get("kind") or "ASSIGN_GOAL"),
            task_id=data.get("task_id"),
            status=status,
            handle=str(data.get("handle") or ""),
            attempts=int(data.get("attempts") or 0),
            dispatched_at=_parse(data.get("dispatched_at")),
            next_attempt_at=_parse(data.get("next_attempt_at")),
            error=str(data.get("error") or ""),
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass
class Agent:
    """The record of one Project Agent, and which Project it owns."""

    project_id: str
    runtime: str
    status: AgentStatus = AgentStatus.STARTING
    endpoint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_id: str = field(default_factory=new_agent_id)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def assignment(self) -> AgentAssignment | None:
        """The hand-over this Agent is currently working on, if any."""
        return AgentAssignment.from_dict(self.metadata.get("assignment"))

    def with_assignment(self, assignment: AgentAssignment | None) -> "Agent":
        """Record (or clear) the current hand-over on this Agent."""
        metadata = {k: v for k, v in self.metadata.items() if k != "assignment"}
        if assignment is not None:
            metadata["assignment"] = assignment.to_dict()
        self.metadata = metadata
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of this agent."""
        return {
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "runtime": self.runtime,
            "status": self.status.value,
            "endpoint": self.endpoint,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class ProjectAgentConfig:
    """What a Project Agent is given when it is started.

    This is the whole contract: a goal, its context, its limits, somewhere to
    work, and how to call NEXUS SEED back.  Project Agents are generic — there
    is no per-project agent code.

    What the Agent *can do* is deliberately absent.  Its skills and tools are
    its own configuration; NEXUS SEED delegates a goal, not a method.
    """

    agent_id: str
    project_id: str
    goal: str
    project_context: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    workspace: str | None = None
    nexus_seed_a2a_endpoint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form handed to the agent runtime."""
        return {
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "goal": self.goal,
            "project_context": self.project_context,
            "constraints": self.constraints,
            "workspace": self.workspace,
            "nexus_seed_a2a_endpoint": self.nexus_seed_a2a_endpoint,
        }


class RoutingAction(str, Enum):
    """What the ProjectRouter decided to do with an incoming request."""

    CREATE_PROJECT = "CREATE_PROJECT"
    ADD_TASK_TO_PROJECT = "ADD_TASK_TO_PROJECT"
    UPDATE_PROJECT = "UPDATE_PROJECT"
    IGNORE = "IGNORE"


@dataclass
class RoutingDecision:
    """The ProjectRouter's answer for one incoming request."""

    action: RoutingAction
    target_project_id: str | None = None
    proposed_goal: str | None = None
    proposed_task: str | None = None
    reason: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of this decision."""
        return {
            "action": self.action.value,
            "target_project_id": self.target_project_id,
            "proposed_goal": self.proposed_goal,
            "proposed_task": self.proposed_task,
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass
class RoutingContext:
    """Everything the ProjectRouter is allowed to reason over.

    Compiled by the ContextManager for one routing decision; it is a temporary
    view, never a source of truth.
    """

    request: str
    source: str = "user"
    world_state: dict[str, Any] = field(default_factory=dict)
    active_projects: list[dict[str, Any]] = field(default_factory=list)
    recent_project_summaries: list[dict[str, Any]] = field(default_factory=list)
    user_context: dict[str, Any] = field(default_factory=dict)
    origin_project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form handed to the reasoning backend."""
        return {
            "request": self.request,
            "source": self.source,
            "world_state": self.world_state,
            "active_projects": self.active_projects,
            "recent_project_summaries": self.recent_project_summaries,
            "user_context": self.user_context,
            "origin_project_id": self.origin_project_id,
        }


class A2AMessageType(str, Enum):
    """The messages a Project Agent may send back to NEXUS SEED.

    Progress and completion are reports; everything else is an escalation —
    the Agent could not continue on its own.
    """

    PROJECT_STATUS = "PROJECT_STATUS"
    PROJECT_COMPLETED = "PROJECT_COMPLETED"
    NEED_HUMAN_INPUT = "NEED_HUMAN_INPUT"
    NEED_CAPABILITY = "NEED_CAPABILITY"
    NEED_RESOURCE = "NEED_RESOURCE"
    NEED_PERMISSION = "NEED_PERMISSION"
    PROJECT_BLOCKED = "PROJECT_BLOCKED"
    DISCOVERED_NEW_PROJECT = "DISCOVERED_NEW_PROJECT"


#: Escalations that leave a Project BLOCKED until NEXUS SEED resolves them.
BLOCKING_ESCALATIONS = frozenset(
    {
        A2AMessageType.NEED_CAPABILITY,
        A2AMessageType.NEED_RESOURCE,
        A2AMessageType.NEED_PERMISSION,
        A2AMessageType.PROJECT_BLOCKED,
    }
)


@dataclass
class A2AMessage:
    """One message on the NEXUS SEED <-> Project Agent channel."""

    type: A2AMessageType
    project_id: str | None = None
    source_agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of this message."""
        return {
            "id": self.id,
            "source_agent_id": self.source_agent_id,
            "project_id": self.project_id,
            "type": self.type.value,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }


__all__ = [
    "AGENT_ID_PREFIX",
    "ROUTING_LIST_LIMIT",
    "ROUTING_TEXT_LIMIT",
    "OPEN_ASSIGNMENTS",
    "PROJECT_ID_PREFIX",
    "A2AMessage",
    "A2AMessageType",
    "BLOCKING_ESCALATIONS",
    "Agent",
    "AgentAssignment",
    "AgentStatus",
    "AssignmentStatus",
    "LIVE_STATUSES",
    "Project",
    "ProjectAgentConfig",
    "ProjectStatus",
    "RoutingAction",
    "RoutingContext",
    "RoutingDecision",
    "TERMINAL_STATUSES",
    "clip",
    "new_agent_id",
    "new_project_id",
]
