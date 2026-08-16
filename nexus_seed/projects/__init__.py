"""Project Situation is a read-only projection, never a Core primitive."""

from .models import ProjectOverallStatus, ProjectSituation, ProjectSituationSummary
from .projections import (
    get_project_situation,
    get_project_situations,
    get_project_summaries,
)

__all__ = [
    "ProjectOverallStatus",
    "ProjectSituation",
    "ProjectSituationSummary",
    "get_project_situation",
    "get_project_situations",
    "get_project_summaries",
]
