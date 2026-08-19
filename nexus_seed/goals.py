"""Creating and ending a Goal, without a command surface behind it.

Goals used to exist only because ``/goal create`` could make one.  That command
is gone, so the two things it actually did — write the record and say so with
an Event — live here, reachable from any channel.

This module is deliberately small and deliberately temporary: a Goal is now
almost exactly an orchestrator Project's objective, and the Project Orchestrator
is where new work is meant to start.  What stays after Goals go is the shape of
this file, not the file.
"""

from __future__ import annotations

from typing import Any, Iterable

from .control.models import Goal, GoalStatus, WorkConstraints, WorkPriority
from .core.event import Event
from .projects.lifecycle import attach_project


async def create_goal(
    runtime,
    objective: str,
    *,
    title: str | None = None,
    owner: str = "human",
    priority: WorkPriority | str = WorkPriority.NORMAL,
    success_criteria: Iterable[Any] = (),
    constraints: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
) -> Goal:
    """Record a Goal, attach its Project, and announce it.

    Creating a Goal is creating its Project: only the association is written,
    because the Goal stays the source of title, objective and status and the
    derived id keeps the mapping one-to-one across restarts.
    """

    text = (objective or "").strip()
    if not text:
        raise ValueError("a goal objective is required")
    goal = Goal(
        title=(title or text).strip(),
        objective=text,
        owner_identity_id=owner,
        scope=dict(scope or {}),
        priority=WorkPriority(str(priority.value if isinstance(priority, WorkPriority) else priority).upper()),
        constraints=WorkConstraints.from_dict(constraints or {}),
        success_criteria=list(success_criteria),
        metadata=dict(metadata or {}),
    )
    project_id = attach_project(goal)
    runtime.control_store.save_goal(goal)
    await runtime.submit_event(
        Event(
            type="goal_created",
            source=f"human:{owner}",
            payload={"goal_id": str(goal.id), "project_id": project_id},
        )
    )
    return goal


async def end_goal(
    runtime, goal_id, status: GoalStatus | str, *, owner: str = "human"
) -> Goal | None:
    """Move a Goal to a new lifecycle status, or ``None`` when it is unknown.

    Only the status change and its Event happen here.  What a cancelled Goal
    means for the Work underneath it belongs to whatever owns that Work.
    """

    goal = runtime.control_store.get_goal(goal_id)
    if goal is None:
        return None
    value = GoalStatus(status.value if isinstance(status, GoalStatus) else str(status).upper())
    runtime.control_store.update_goal_status(goal.id, value)
    past = {
        GoalStatus.PAUSED: "paused",
        GoalStatus.ACTIVE: "resumed",
        GoalStatus.CANCELLED: "cancelled",
        GoalStatus.ACHIEVED: "achieved",
    }[value]
    await runtime.submit_event(
        Event(
            type=f"goal_{past}",
            source=f"human:{owner}",
            payload={"goal_id": str(goal.id)},
        )
    )
    return runtime.control_store.get_goal(goal.id)


__all__ = ["create_goal", "end_goal"]
