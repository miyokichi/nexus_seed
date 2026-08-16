"""A Project is one Goal and its Work, projected — never a Core primitive."""

from .lifecycle import (
    attach_project,
    goal_id_from_project_id,
    project_id_for_goal,
    project_id_of,
)
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
    project_status,
)

__all__ = [
    "Project",
    "ProjectOverallStatus",
    "ProjectSituation",
    "ProjectSituationSummary",
    "attach_project",
    "get_project_situation",
    "get_project_situations",
    "get_project_summaries",
    "goal_id_from_project_id",
    "project_id_for_goal",
    "project_id_of",
    "project_status",
]
