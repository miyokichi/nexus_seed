"""Goal-rooted project identity.

A Project is one Goal plus the Work that Goal generates.  It is therefore not a
record of its own: creating a Goal *is* creating its Project, and the only thing
written is the Goal's own ``project_id`` association.  Nothing is copied — the
title, objective and lifecycle are read from the root Goal whenever a caller
asks (Invariants 196–199).

The identifier is derived from the Goal id, which is what makes the mapping
idempotent: re-running creation, restarting, or recompiling the projection all
arrive at the same single Project for that Goal.
"""

from __future__ import annotations

import uuid
from typing import Any

#: Prefix of an automatically derived, Goal-rooted project identifier.
PROJECT_ID_PREFIX = "project-"


def project_id_for_goal(goal_id: uuid.UUID | str) -> str:
    """Return the one project identifier belonging to ``goal_id``."""

    return f"{PROJECT_ID_PREFIX}{goal_id}"


def goal_id_from_project_id(project_id: str) -> uuid.UUID | None:
    """Return the root Goal id encoded in a derived project id, if it is one."""

    if not project_id.startswith(PROJECT_ID_PREFIX):
        return None
    try:
        return uuid.UUID(project_id[len(PROJECT_ID_PREFIX) :])
    except ValueError:
        return None


def project_id_of(goal) -> str | None:
    """Return the project a Goal already belongs to, without inventing one."""

    metadata = getattr(goal, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("project_id")
    return str(value).strip() or None if value is not None else None


def attach_project(goal) -> str:
    """Associate ``goal`` with its Project, and return that project id.

    An explicit ``project_id`` supplied by the caller is kept, so an existing
    Goal creation API keeps working exactly as before.  Otherwise the Goal is
    given its own derived project.  Only the association is stored; the Goal
    stays the single source of the title, objective and status.
    """

    existing = project_id_of(goal)
    if existing is not None:
        return existing
    project_id = project_id_for_goal(goal.id)
    goal.metadata["project_id"] = project_id
    return project_id


def is_derived(project_id: str, goal_id: uuid.UUID | str) -> bool:
    """Whether ``project_id`` is the project derived from ``goal_id``."""

    return project_id == project_id_for_goal(goal_id)


def root_goal(project_id: str, goals: list[Any]):
    """Return the Goal a project is rooted in, or ``None`` when it has none.

    A Goal that owns its derived project always wins.  Otherwise — a project
    assembled from explicit metadata, where several Goals may name it — the
    oldest Goal is the root, so the answer does not change between reads.
    """

    associated = [goal for goal in goals if project_id_of(goal) == project_id]
    if not associated:
        return None
    for goal in associated:
        if is_derived(project_id, goal.id):
            return goal
    return min(associated, key=lambda goal: (goal.created_at, str(goal.id)))


__all__ = [
    "PROJECT_ID_PREFIX",
    "attach_project",
    "goal_id_from_project_id",
    "is_derived",
    "project_id_for_goal",
    "project_id_of",
    "root_goal",
]
