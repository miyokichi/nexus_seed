"""Read the whole Goal loop in one place, without owning any of it.

This module coordinates; it does not decide.  Every fact here already exists in
a subsystem that owns it — Goal and Project, Work Planning, Capability
Acquisition, Execution, Evaluation — and is read through that subsystem's own
projection or store.  Nothing is recomputed, duplicated or written
(Invariants 204–206).
"""

from __future__ import annotations

from typing import Any
import uuid

from ..autonomy.models import AcquisitionStatus
from ..control.models import GoalStatus
from ..projects.lifecycle import project_id_of
from ..projects.models import ProjectOverallStatus
from ..projects.projections import get_project_situation
from .models import GoalLoopStage, GoalLoopStatus

#: Acquisition states that mean automatic work is still going on.
ACQUIRING = frozenset(
    {
        AcquisitionStatus.ANALYZING,
        AcquisitionStatus.CONSTRUCTING,
        AcquisitionStatus.INSTALLING,
    }
)
#: Acquisition states that mean the loop has stopped and a person is needed.
NEEDS_HUMAN = frozenset(
    {
        AcquisitionStatus.WAITING_REVIEW,
        AcquisitionStatus.BLOCKED,
        AcquisitionStatus.FAILED,
    }
)


def get_goal_loop(runtime, goal_id) -> GoalLoopStatus | None:
    """Return where one Goal stands in the loop, or ``None`` when unknown."""

    identifier = _as_uuid(goal_id)
    goal = runtime.control_store.get_goal(identifier) if identifier else None
    if goal is None:
        return None
    project_id = project_id_of(goal)
    if project_id is None:
        return None
    situation = get_project_situation(runtime, project_id)
    return _compile(runtime, goal, situation) if situation is not None else None


def get_goal_loops(runtime) -> list[GoalLoopStatus]:
    """Return every Goal-rooted loop, in deterministic project order."""

    loops = []
    for goal in runtime.control_store.goals():
        project_id = project_id_of(goal)
        if project_id is None:
            continue
        situation = get_project_situation(runtime, project_id)
        if situation is None or situation.project is None:
            continue
        if situation.project.root_goal_id != str(goal.id):
            continue
        loops.append(_compile(runtime, goal, situation))
    return sorted(loops, key=lambda item: (item.project_id, item.goal_id))


def _compile(runtime, goal, situation) -> GoalLoopStatus:
    works = runtime.work_requirement_store.for_goal(goal.id)
    acquisitions = _acquisitions(runtime, works)
    waiting_for: list[dict[str, Any]] = []
    human_requests: list[dict[str, Any]] = []

    for review in situation.pending_reviews:
        human_requests.append(
            {
                "kind": "review",
                "id": review["id"],
                "event_type": review["event_type"],
                "summary": f"{review['event_type']} requires a human decision",
            }
        )
    for question in situation.unresolved_questions:
        human_requests.append(
            {
                "kind": "question",
                "id": question["id"],
                "summary": question["text"],
            }
        )
    for item in acquisitions:
        if item["status"] in {status.value for status in NEEDS_HUMAN}:
            human_requests.append(
                {
                    "kind": "capability",
                    "id": item["acquisition_session_id"],
                    "work_id": item["work_id"],
                    "missing_capabilities": item["missing_capabilities"],
                    "summary": item["blocked_reason"]
                    or "automatic capability acquisition cannot continue",
                }
            )
        elif item["status"] in {status.value for status in ACQUIRING}:
            waiting_for.append(
                {
                    "kind": "capability_acquisition",
                    "id": item["acquisition_session_id"],
                    "work_id": item["work_id"],
                    "summary": f"acquiring {', '.join(item['missing_capabilities']) or 'a capability'}",
                }
            )
    for item in situation.blocked_tasks:
        waiting_for.append(
            {
                "kind": "blocked_work",
                "id": item["id"],
                "status": item["status"],
                "summary": item["objective"] or item["type"],
            }
        )
    for item in situation.remaining_tasks:
        waiting_for.append(
            {
                "kind": "work",
                "id": item["id"],
                "status": item["status"],
                "summary": item["objective"] or item["type"],
            }
        )

    missing = sorted(
        {
            name
            for item in acquisitions
            for name in item["missing_capabilities"]
        }
        | {name for item in situation.blocked_tasks for name in item.get("missing_capabilities", ())}
    )
    stage = _stage(
        goal=goal,
        situation=situation,
        works=works,
        acquisitions=acquisitions,
        human_requests=human_requests,
    )
    return GoalLoopStatus(
        goal_id=str(goal.id),
        project_id=situation.project_id,
        title=situation.title,
        objective=situation.objective,
        stage=stage,
        goal_status=goal.status.value,
        project_status=situation.overall_status.value,
        summary=_summary(stage, situation, missing, human_requests),
        remaining_tasks=len(situation.remaining_tasks),
        blocked_tasks=len(situation.blocked_tasks),
        completed_tasks=situation.completed_total,
        missing_capabilities=tuple(missing),
        acquisitions=tuple(acquisitions),
        waiting_for=tuple(waiting_for),
        human_requests=tuple(human_requests),
        updated_at=situation.updated_at,
    )


def _acquisitions(runtime, works) -> list[dict[str, Any]]:
    """Read the Capability subsystem's own records for this Goal's Work."""

    result: list[dict[str, Any]] = []
    for work in works:
        gaps = runtime.get_capability_gaps_for_work(work.id)
        for session in runtime.autonomy_store.sessions_for_work(work.id):
            result.append(
                {
                    "acquisition_session_id": str(session.id),
                    "work_id": str(work.id),
                    "status": session.status.value,
                    "stage": session.current_stage.value,
                    "blocked_reason": session.blocked_reason,
                    "missing_capabilities": sorted(
                        {
                            *work.missing_capabilities,
                            *(name for gap in gaps for name in gap.missing_names),
                        }
                    ),
                }
            )
    return result


def _stage(*, goal, situation, works, acquisitions, human_requests) -> GoalLoopStage:
    """Decide the one stage these facts imply, in a fixed order.

    The Goal's lifecycle comes first, then whether a person is needed, then
    whether the machine is still working, and only then how far the Work has
    got.  The same records always produce the same stage.
    """

    if goal.status is GoalStatus.CANCELLED:
        return GoalLoopStage.CANCELLED
    if goal.status is GoalStatus.PAUSED:
        return GoalLoopStage.PAUSED
    if goal.status is GoalStatus.ACHIEVED:
        return GoalLoopStage.ACHIEVED
    if human_requests:
        return GoalLoopStage.HUMAN_REQUIRED
    if any(item["status"] in {status.value for status in ACQUIRING} for item in acquisitions):
        return GoalLoopStage.ACQUIRING_CAPABILITY
    if situation.overall_status is ProjectOverallStatus.BLOCKED:
        return GoalLoopStage.BLOCKED
    if situation.remaining_tasks:
        return GoalLoopStage.EXECUTING
    if works:
        return GoalLoopStage.EVALUATING
    return GoalLoopStage.PLANNING


def _summary(stage, situation, missing, human_requests) -> str:
    """One human-readable sentence about where the loop is."""

    title = situation.title
    if stage is GoalLoopStage.ACHIEVED:
        return f"{title} is achieved; the project is complete."
    if stage is GoalLoopStage.CANCELLED:
        return f"{title} was cancelled."
    if stage is GoalLoopStage.PAUSED:
        return f"{title} is paused."
    if stage is GoalLoopStage.HUMAN_REQUIRED:
        first = human_requests[0]["summary"]
        return f"{title} needs a person: {first}"
    if stage is GoalLoopStage.ACQUIRING_CAPABILITY:
        names = ", ".join(missing) or "a missing capability"
        return f"{title} is acquiring {names}."
    if stage is GoalLoopStage.BLOCKED:
        blocker = situation.blockers[0]["summary"] if situation.blockers else "an unresolved blocker"
        return f"{title} is blocked: {blocker}"
    if stage is GoalLoopStage.EXECUTING:
        return f"{title} has {len(situation.remaining_tasks)} task(s) in progress."
    if stage is GoalLoopStage.EVALUATING:
        return f"{title} is being re-evaluated against the current world state."
    return f"{title} has no work yet; the gap is still being planned."


def _as_uuid(value) -> uuid.UUID | None:
    """Accept a Goal id in either form; refuse anything that is not one."""

    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


__all__ = ["ACQUIRING", "NEEDS_HUMAN", "get_goal_loop", "get_goal_loops"]
