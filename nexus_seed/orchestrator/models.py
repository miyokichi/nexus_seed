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
            "tasks": list(self.tasks),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def to_routing_dict(self) -> dict[str, Any]:
        """Return the compact form the ProjectRouter is asked to reason over."""
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status.value,
            "priority": self.priority,
            "summary": self.summary,
            "open_tasks": [task.get("description", "") for task in self.tasks],
            "blockers": [blocker.get("reason", "") for blocker in self.blockers],
        }


class AgentStatus(str, Enum):
    """Lifecycle of a Project Agent as NEXUS SEED sees it."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    IDLE = "IDLE"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


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
    work, what it may use, and how to call NEXUS SEED back.  Project Agents are
    generic — there is no per-project agent code.
    """

    agent_id: str
    project_id: str
    goal: str
    project_context: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    workspace: str | None = None
    available_skills: tuple[str, ...] = ()
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
            "available_skills": list(self.available_skills),
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
    "PROJECT_ID_PREFIX",
    "A2AMessage",
    "A2AMessageType",
    "BLOCKING_ESCALATIONS",
    "Agent",
    "AgentStatus",
    "LIVE_STATUSES",
    "Project",
    "ProjectAgentConfig",
    "ProjectStatus",
    "RoutingAction",
    "RoutingContext",
    "RoutingDecision",
    "TERMINAL_STATUSES",
    "new_agent_id",
    "new_project_id",
]
