"""Read-only project situation values reconstructed from durable state.

These are domain projection records, not Core primitives and not persistence
models.  A fresh instance is compiled whenever a caller asks for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class ProjectOverallStatus(str, Enum):
    """How a project reads to a person, in one word."""

    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    ACTIVE = "ACTIVE"
    PLANNING = "PLANNING"
    COMPLETED = "COMPLETED"
    IDLE = "IDLE"


@dataclass(frozen=True, slots=True)
class Project:
    """The thin management record naming one project.

    Kept because the situation's JSON shape is a public surface; the durable
    record itself is the orchestrator's :class:`nexus_seed.orchestrator.Project`.
    """

    project_id: str
    title: str
    objective: str
    status: ProjectOverallStatus
    root_goal_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of the project record."""

        return {
            "project_id": self.project_id,
            "root_goal_id": self.root_goal_id,
            "title": self.title,
            "objective": self.objective,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(frozen=True, slots=True)
class ProjectSituation:
    """One regenerable, project-scoped view over existing durable records."""

    project_id: str
    title: str
    objective: str
    overall_status: ProjectOverallStatus
    project: Project | None = None
    goal: dict[str, Any] | None = None
    current_intention: dict[str, Any] | None = None
    active_goals: tuple[dict[str, Any], ...] = ()
    current_intentions: tuple[dict[str, Any], ...] = ()
    active_work: tuple[dict[str, Any], ...] = ()
    blocked_work: tuple[dict[str, Any], ...] = ()
    recently_completed_work: tuple[dict[str, Any], ...] = ()
    pending_reviews: tuple[dict[str, Any], ...] = ()
    recent_events: tuple[dict[str, Any], ...] = ()
    unresolved_questions: tuple[dict[str, Any], ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    recent_changes: tuple[dict[str, Any], ...] = ()
    updated_at: datetime | None = None
    summary: str = ""
    #: Every satisfied Work, counted before ``recently_completed_work`` is
    #: trimmed for display, so a compact summary still reports the real total.
    completed_total: int = 0

    @property
    def remaining_tasks(self) -> tuple[dict[str, Any], ...]:
        """The Work still to be done for the root Goal."""

        return self.active_work

    @property
    def blocked_tasks(self) -> tuple[dict[str, Any], ...]:
        """The Work that cannot currently proceed."""

        return self.blocked_work

    @property
    def completed_tasks(self) -> tuple[dict[str, Any], ...]:
        """The Work that has been satisfied."""

        return self.recently_completed_work

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation used by HTTP and LLM callers."""

        return {
            "project_id": self.project_id,
            "title": self.title,
            "objective": self.objective,
            "overall_status": self.overall_status.value,
            "project": self.project.to_dict() if self.project else None,
            "goal": self.goal,
            "current_intention": self.current_intention,
            "remaining_tasks": list(self.remaining_tasks),
            "blocked_tasks": list(self.blocked_tasks),
            "completed_tasks": list(self.completed_tasks),
            "active_goals": list(self.active_goals),
            "current_intentions": list(self.current_intentions),
            "active_work": list(self.active_work),
            "blocked_work": list(self.blocked_work),
            "recently_completed_work": list(self.recently_completed_work),
            "completed_total": self.completed_total,
            "pending_reviews": list(self.pending_reviews),
            "recent_events": list(self.recent_events),
            "unresolved_questions": list(self.unresolved_questions),
            "blockers": list(self.blockers),
            "recent_changes": list(self.recent_changes),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class ProjectSituationSummary:
    """Compact list representation derived from a full ProjectSituation."""

    project_id: str
    title: str
    status: ProjectOverallStatus
    active_goals: int
    active_work: int
    blocked_work: int
    pending_reviews: int
    needs_attention: bool
    updated_at: datetime | None = None
    root_goal_id: str | None = None
    objective: str = ""
    completed_work: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation for ``GET /projects``."""

        return {
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status.value,
            "root_goal_id": self.root_goal_id,
            "objective": self.objective,
            "active_goals": self.active_goals,
            "active_work": self.active_work,
            "blocked_work": self.blocked_work,
            "completed_work": self.completed_work,
            "remaining_tasks": self.active_work,
            "blocked_tasks": self.blocked_work,
            "completed_tasks": self.completed_work,
            "pending_reviews": self.pending_reviews,
            "needs_attention": self.needs_attention,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "Project",
    "ProjectOverallStatus",
    "ProjectSituation",
    "ProjectSituationSummary",
]
