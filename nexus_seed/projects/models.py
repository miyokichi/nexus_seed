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
    """Deterministic status derived from existing Goal/Work/Review state."""

    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    IDLE = "IDLE"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class ProjectSituation:
    """One regenerable, project-scoped view over existing durable records."""

    project_id: str
    title: str
    objective: str
    overall_status: ProjectOverallStatus
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

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation used by HTTP and LLM callers."""

        return {
            "project_id": self.project_id,
            "title": self.title,
            "objective": self.objective,
            "overall_status": self.overall_status.value,
            "active_goals": list(self.active_goals),
            "current_intentions": list(self.current_intentions),
            "active_work": list(self.active_work),
            "blocked_work": list(self.blocked_work),
            "recently_completed_work": list(self.recently_completed_work),
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

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation for ``GET /projects``."""

        return {
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status.value,
            "active_goals": self.active_goals,
            "active_work": self.active_work,
            "blocked_work": self.blocked_work,
            "pending_reviews": self.pending_reviews,
            "needs_attention": self.needs_attention,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "ProjectOverallStatus",
    "ProjectSituation",
    "ProjectSituationSummary",
]
