"""A Project as people read it — never a Core primitive."""

from .models import (
    Project,
    ProjectOverallStatus,
    ProjectSituation,
    ProjectSituationSummary,
)
from .projections import (
    get_project_situation,
    get_project_situations,
    get_project_summaries,
)

__all__ = [
    "Project",
    "ProjectOverallStatus",
    "ProjectSituation",
    "ProjectSituationSummary",
    "get_project_situation",
    "get_project_situations",
    "get_project_summaries",
]
