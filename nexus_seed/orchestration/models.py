"""Where one Goal stands in the NEXUS SEED loop.

The loop itself is not new machinery.  It is the existing subsystems, read in
one order::

    Goal -> Project -> World -> Work -> Capability -> Execution -> Evaluation -> Goal

``GoalLoopStatus`` is the single place that answers "where is this Goal right
now, and what is it waiting for?" without any subsystem having to know about
the others.  It is compiled on demand from durable records and stores nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class GoalLoopStage(str, Enum):
    """The one stage a Goal currently occupies in the loop."""

    #: The Project exists and the gap has not yet become Work.
    PLANNING = "PLANNING"
    #: Work exists and is being matched, spawned or run.
    EXECUTING = "EXECUTING"
    #: Work is held while the missing Capability is being acquired.
    ACQUIRING_CAPABILITY = "ACQUIRING_CAPABILITY"
    #: NEXUS SEED cannot continue on its own and has said so.
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    #: Something hard stops progress that is neither acquisition nor a human ask.
    BLOCKED = "BLOCKED"
    #: Every Work is satisfied; the Goal is being re-evaluated against the world.
    EVALUATING = "EVALUATING"
    #: The Goal is achieved, so the Project is complete.
    ACHIEVED = "ACHIEVED"
    #: The root Goal is paused or cancelled through the Control Plane.
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        """Whether the loop has stopped turning for this Goal."""

        return self in {
            GoalLoopStage.ACHIEVED,
            GoalLoopStage.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class GoalLoopStatus:
    """One Goal's position in the loop, compiled from existing records."""

    goal_id: str
    project_id: str
    title: str
    objective: str
    stage: GoalLoopStage
    goal_status: str
    project_status: str
    summary: str
    remaining_tasks: int = 0
    blocked_tasks: int = 0
    completed_tasks: int = 0
    missing_capabilities: tuple[str, ...] = ()
    acquisitions: tuple[dict[str, Any], ...] = ()
    waiting_for: tuple[dict[str, Any], ...] = ()
    human_requests: tuple[dict[str, Any], ...] = ()
    updated_at: datetime | None = None

    @property
    def needs_human(self) -> bool:
        """Whether a person has to do something before the loop can continue."""

        return self.stage is GoalLoopStage.HUMAN_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation used by HTTP and the Cockpit."""

        return {
            "goal_id": self.goal_id,
            "project_id": self.project_id,
            "title": self.title,
            "objective": self.objective,
            "stage": self.stage.value,
            "goal_status": self.goal_status,
            "project_status": self.project_status,
            "summary": self.summary,
            "remaining_tasks": self.remaining_tasks,
            "blocked_tasks": self.blocked_tasks,
            "completed_tasks": self.completed_tasks,
            "missing_capabilities": list(self.missing_capabilities),
            "acquisitions": list(self.acquisitions),
            "waiting_for": list(self.waiting_for),
            "human_requests": list(self.human_requests),
            "needs_human": self.needs_human,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["GoalLoopStage", "GoalLoopStatus"]
