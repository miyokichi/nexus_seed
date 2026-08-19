"""One project's situation, compiled from the Project Orchestrator's records.

A Project *is* the goal: the objective a person handed over, delegated whole to
one Agent.  There is nothing to reconstruct it from — no Goal to join to Work
to Processes — because NEXUS SEED did not do the work.  What it knows is what
it delegated, what it handed over since, what the Agent reported, and what is
in the way; that is what a situation says, and it deliberately says no more.

This module is the stable name the rest of the system asks through
(``ctx.services.get_project_situation``, ``GET /projects/{id}/situation``,
Project Chat, the Cockpit).  The compilation itself lives beside the records,
in :mod:`nexus_seed.orchestrator.situation`.
"""

from __future__ import annotations

from ..orchestrator.situation import (
    orchestrator_situation,
    orchestrator_situations,
)
from .models import ProjectOverallStatus, ProjectSituation, ProjectSituationSummary


#: Statuses that mean a person should look at this project.
NEEDS_ATTENTION = {ProjectOverallStatus.BLOCKED, ProjectOverallStatus.NEEDS_ATTENTION}


def get_project_situation(
    runtime,
    project_id: str,
    *,
    recent_limit: int = 20,
) -> ProjectSituation | None:
    """Compile one project situation, or return ``None`` when unknown."""

    identifier = str(project_id or "").strip()
    if not identifier:
        return None
    return orchestrator_situation(runtime, identifier, recent_limit=recent_limit)


def get_project_situations(
    runtime,
    *,
    recent_limit: int = 20,
) -> list[ProjectSituation]:
    """Compile every project situation, in deterministic project order."""

    return orchestrator_situations(runtime, recent_limit=recent_limit)


def get_project_summaries(runtime) -> list[ProjectSituationSummary]:
    """Return compact summaries for listing every project."""

    return [
        ProjectSituationSummary(
            project_id=item.project_id,
            title=item.title,
            status=item.overall_status,
            active_goals=1,
            active_work=len(item.remaining_tasks),
            blocked_work=len(item.blocked_tasks),
            pending_reviews=len(item.pending_reviews),
            needs_attention=item.overall_status in NEEDS_ATTENTION,
            updated_at=item.updated_at,
            objective=item.objective,
            completed_work=item.completed_total,
        )
        for item in get_project_situations(runtime, recent_limit=1)
    ]


__all__ = [
    "NEEDS_ATTENTION",
    "get_project_situation",
    "get_project_situations",
    "get_project_summaries",
]
