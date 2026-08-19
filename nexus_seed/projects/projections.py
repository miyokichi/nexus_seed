"""Project-scoped read projections over existing NEXUS SEED stores.

A Project is one root Goal plus the Work that Goal generates, reconstructed
here from the records those two already leave behind.  Association is
intentionally conservative: only explicit ``project`` / ``project_id``
metadata, a Goal/Work/Intention identifier link, or an existing causal /
provenance link is followed.  Text similarity and LLM inference never assign a
record to a project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import uuid
from typing import Any, Iterable

from ..control.models import GoalStatus
from ..orchestrator.situation import orchestrator_situation, orchestrator_situations
from ..core.process import ProcessStatus
from ..presence.models import IntentionStatus, self_question_id
from ..presence.projections import get_intentions
from ..work.work_requirement import WorkStatus
from .lifecycle import root_goal as _root_goal
from .models import (
    Project,
    ProjectOverallStatus,
    ProjectSituation,
    ProjectSituationSummary,
)


ACTIVE_WORK = frozenset(
    {
        WorkStatus.EXPECTED,
        WorkStatus.MATCHED,
        WorkStatus.SPAWNED,
        WorkStatus.PLANNED,
        WorkStatus.PAUSED,
        WorkStatus.WAITING_REVIEW,
    }
)
BLOCKED_WORK = frozenset(
    {
        WorkStatus.BLOCKED_CAPABILITY,
        WorkStatus.BLOCKED_PROVIDER,
        WorkStatus.BLOCKED_PLAN,
    }
)


@dataclass(slots=True)
class _ProjectionSource:
    goals: list
    intentions: list
    works: list
    events: list
    processes: list
    continuations: list
    state_entries: list
    state_history: list


def get_project_situation(
    runtime,
    project_id: str,
    *,
    recent_limit: int = 20,
) -> ProjectSituation | None:
    """Reconstruct one project situation, or return ``None`` when unknown."""

    normalized = _identifier(project_id)
    if normalized is None:
        return None
    source = _read_source(runtime)
    catalog = _project_catalog(source)
    if normalized not in catalog:
        # An orchestrator Project is a durable record rather than something
        # derived from Goals, so it is looked up rather than reconstructed.
        # Callers — Project Chat, the HTTP situation route, the Cockpit — do
        # not need to know which kind of project they were handed.
        return orchestrator_situation(runtime, normalized, recent_limit=recent_limit)
    return _compile(runtime, source, normalized, catalog[normalized], recent_limit)


def get_project_situations(
    runtime,
    *,
    recent_limit: int = 20,
) -> list[ProjectSituation]:
    """Reconstruct every explicitly identified project in deterministic order."""

    source = _read_source(runtime)
    catalog = _project_catalog(source)
    return [
        _compile(runtime, source, project_id, catalog[project_id], recent_limit)
        for project_id in sorted(catalog)
    ]


def get_project_summaries(
    runtime, *, include_orchestrator: bool = True
) -> list[ProjectSituationSummary]:
    """Return compact summaries of every project a person could be talking about.

    Orchestrator Projects are included by default because callers that ask
    "which projects exist?" — the chat scope guard above all — must not treat a
    real project as unknown.  The Cockpit's Goal-Projects list passes
    ``include_orchestrator=False`` so the two kinds stay visibly apart while
    both still exist.
    """

    situations = list(get_project_situations(runtime, recent_limit=1))
    if include_orchestrator:
        situations += orchestrator_situations(runtime, recent_limit=1)
    return [
        ProjectSituationSummary(
            project_id=item.project_id,
            title=item.title,
            status=item.overall_status,
            active_goals=len(item.active_goals),
            active_work=len(item.remaining_tasks),
            blocked_work=len(item.blocked_tasks),
            pending_reviews=len(item.pending_reviews),
            needs_attention=item.overall_status
            in {ProjectOverallStatus.BLOCKED, ProjectOverallStatus.NEEDS_ATTENTION},
            updated_at=item.updated_at,
            root_goal_id=item.project.root_goal_id if item.project else None,
            objective=item.objective,
            completed_work=item.completed_total,
        )
        for item in situations
    ]


def _read_source(runtime) -> _ProjectionSource:
    return _ProjectionSource(
        goals=runtime.control_store.goals(),
        intentions=get_intentions(runtime),
        works=runtime.work_requirement_store.all(),
        events=runtime.event_store.all(),
        processes=runtime.process_store.all_instances(),
        continuations=runtime.continuation_store.all(),
        state_entries=runtime.state_store.all_current(),
        state_history=runtime.state_store.all_history(),
    )


def _project_catalog(source: _ProjectionSource) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}

    def add(project_id: str | None, descriptor: dict[str, Any] | None = None) -> None:
        if project_id is None:
            return
        current = catalog.setdefault(project_id, {})
        for key in ("title", "objective"):
            value = (descriptor or {}).get(key)
            if value and not current.get(key):
                current[key] = str(value)

    for entry in source.state_entries:
        if entry.entity.startswith("project:"):
            add(_identifier(entry.entity.split(":", 1)[1]))
    for goal in source.goals:
        project_id, descriptor = _association(goal.metadata, goal.scope)
        add(project_id, descriptor)
    for work in source.works:
        project_id, descriptor = _association(
            {"project_id": work.project} if work.project else {},
            work.metadata,
            work.scope,
        )
        add(project_id, descriptor)
    for event in source.events:
        project_id, descriptor = _association(event.payload)
        add(project_id, descriptor)
    self_questions = next(
        (
            entry.value
            for entry in source.state_entries
            if entry.entity == "self" and entry.attribute == "unresolved_questions"
        ),
        (),
    )
    for question in _items(self_questions):
        if isinstance(question, dict):
            project_id, descriptor = _association(question)
            add(project_id, descriptor)
    for project_id, descriptor in catalog.items():
        for entry in source.state_entries:
            if entry.entity != f"project:{project_id}":
                continue
            if entry.attribute in {"title", "objective"} and entry.value:
                descriptor[entry.attribute] = str(entry.value)
    return catalog


def _compile(
    runtime,
    source: _ProjectionSource,
    project_id: str,
    descriptor: dict[str, Any],
    recent_limit: int,
) -> ProjectSituation:
    goal_project = {
        goal.id: _association(goal.metadata, goal.scope)[0] for goal in source.goals
    }
    goals = [goal for goal in source.goals if goal_project.get(goal.id) == project_id]
    goal_ids = {goal.id for goal in goals}

    work_project: dict[uuid.UUID, str | None] = {}
    for work in source.works:
        explicit = _association(
            {"project_id": work.project} if work.project else {},
            work.metadata,
            work.scope,
        )[0]
        work_project[work.id] = explicit or goal_project.get(work.goal_id)
    works = [work for work in source.works if work_project.get(work.id) == project_id]
    work_ids = {work.id for work in works}

    intentions = [item for item in source.intentions if item.goal_id in goal_ids]
    intention_ids = {item.id for item in intentions}
    related_ids = {
        *[str(value) for value in goal_ids],
        *[str(value) for value in work_ids],
        *[str(value) for value in intention_ids],
    }

    events = _project_events(
        source.events,
        project_id=project_id,
        related_ids=related_ids,
        source_event_ids={work.source_event_id for work in works if work.source_event_id},
    )
    event_ids = {event.id for event in events}
    project_processes = [
        process
        for process in source.processes
        if process.work_requirement_id in work_ids or process.trigger_event_id in event_ids
    ]
    process_ids = {process.id for process in project_processes}
    reviews = _reviews(
        source.continuations,
        process_by_id={item.id: item for item in source.processes},
        project_process_ids=process_ids,
        related_ids=related_ids,
        event_ids={str(value) for value in event_ids},
    )
    questions = _questions(source.state_entries, project_id)
    history = [
        entry
        for entry in source.state_history
        if entry.entity in {project_id, f"project:{project_id}"}
        or entry.source_event in event_ids
        or _association_from_value(entry.value) == project_id
    ]

    active_goals = [goal for goal in goals if goal.status is GoalStatus.ACTIVE]
    current_intentions = [item for item in intentions if not item.status.terminal]
    active_work = [work for work in works if work.status in ACTIVE_WORK]
    blocked_work = [work for work in works if work.status in BLOCKED_WORK]
    completed_work = [work for work in works if work.status is WorkStatus.SATISFIED]

    blockers = _blockers(
        goals=goals,
        intentions=intentions,
        works=works,
        blocked_work=blocked_work,
        processes=project_processes,
        reviews=reviews,
    )
    root = _root_goal(project_id, goals)
    status = project_status(
        root=root,
        goals=goals,
        works=works,
        active_work=active_work,
        current_intentions=current_intentions,
        blockers=blockers,
        reviews=reviews,
        questions=questions,
    )

    recent_events = [_event(item) for item in reversed(events[-recent_limit:])]
    recent_changes = _recent_changes(events, history, works, recent_limit)
    timestamps = [
        *[item.updated_at for item in goals],
        *[item.updated_at for item in intentions],
        *[item.updated_at for item in works],
        *[item.occurred_at for item in events],
        *[item.created_at for item in history],
        *[
            entry.updated_at
            for entry in source.state_entries
            if entry.entity == f"project:{project_id}"
        ],
    ]
    updated_at = max(timestamps) if timestamps else None
    # The root Goal names the project.  Explicit descriptor metadata still wins,
    # so a project assembled by hand before the Goal-rooted lifecycle keeps the
    # title it was given.
    title = str(
        descriptor.get("title") or (root.title if root is not None else "") or project_id
    )
    objective = str(
        descriptor.get("objective") or (root.objective if root is not None else "")
    )
    summary = _summary(
        title,
        status,
        active_goals=len(active_goals),
        blocked_work=len(blocked_work),
        reviews=len(reviews),
        latest_change=recent_changes[0]["summary"] if recent_changes else None,
    )
    project = Project(
        project_id=project_id,
        root_goal_id=str(root.id) if root is not None else None,
        title=title,
        objective=objective,
        status=status,
        created_at=root.created_at if root is not None else None,
        updated_at=updated_at,
    )
    current_intention = next(
        (
            _intention(item)
            for item in current_intentions
            if root is None or item.goal_id == root.id
        ),
        None,
    )
    return ProjectSituation(
        project_id=project_id,
        title=title,
        objective=objective,
        overall_status=status,
        project=project,
        goal=_goal(root) if root is not None else None,
        current_intention=current_intention,
        completed_total=len(completed_work),
        active_goals=tuple(_goal(item) for item in active_goals),
        current_intentions=tuple(_intention(item) for item in current_intentions),
        active_work=tuple(_work(item) for item in active_work),
        blocked_work=tuple(_work(item) for item in blocked_work),
        recently_completed_work=tuple(
            _work(item) for item in reversed(completed_work[-recent_limit:])
        ),
        pending_reviews=tuple(reviews),
        recent_events=tuple(recent_events),
        unresolved_questions=tuple(questions),
        blockers=tuple(blockers),
        recent_changes=tuple(recent_changes),
        updated_at=updated_at,
        summary=summary,
    )


def project_status(
    *,
    root,
    goals,
    works,
    active_work,
    current_intentions,
    blockers,
    reviews,
    questions,
) -> ProjectOverallStatus:
    """Derive the one project status these durable facts imply.

    The order is the whole rule, and it is fixed so the same facts always give
    the same answer:

    1. ``CANCELLED`` — the root Goal was cancelled; nothing below it matters.
    2. ``PAUSED`` — the root Goal is paused, so its Work is deliberately idle.
    3. ``BLOCKED`` — something hard stops progress (blocked Goal, Intention,
       Work, unresolved dependency or failed Work).
    4. ``NEEDS_ATTENTION`` — a human decision is pending: a Review or an
       unresolved question.
    5. ``ACTIVE`` — Work is under way.
    6. ``PLANNING`` — the root Goal is active but has generated no Work yet.
    7. ``COMPLETED`` — every Goal is terminal and no Work is outstanding.
    8. ``IDLE`` — anything else, including a project with no Goal at all.

    A project is never given a status of its own to keep in step with the Goal:
    pausing, resuming or cancelling the Goal through the Control Plane is
    already the whole of the project lifecycle (Invariant 200).
    """

    if root is not None and root.status is GoalStatus.CANCELLED:
        return ProjectOverallStatus.CANCELLED
    if root is not None and root.status is GoalStatus.PAUSED:
        return ProjectOverallStatus.PAUSED
    if goals and all(goal.status is GoalStatus.CANCELLED for goal in goals):
        return ProjectOverallStatus.CANCELLED
    if any(item["severity"] == "blocked" for item in blockers):
        return ProjectOverallStatus.BLOCKED
    if reviews or questions:
        return ProjectOverallStatus.NEEDS_ATTENTION
    if active_work:
        return ProjectOverallStatus.ACTIVE
    if root is not None and root.status is GoalStatus.ACTIVE and not works:
        return ProjectOverallStatus.PLANNING
    if [goal for goal in goals if goal.status is GoalStatus.ACTIVE] or current_intentions:
        return ProjectOverallStatus.ACTIVE
    if goals and all(goal.status.terminal for goal in goals):
        return ProjectOverallStatus.COMPLETED
    return ProjectOverallStatus.IDLE


def _project_events(events, *, project_id, related_ids, source_event_ids):
    selected = {
        event.id
        for event in events
        if _association(event.payload)[0] == project_id
        or event.id in source_event_ids
        or bool(_reference_values(event.payload) & related_ids)
    }
    correlations = {
        event.correlation_id
        for event in events
        if event.id in selected and event.correlation_id is not None
    }
    changed = True
    while changed:
        changed = False
        for event in events:
            if event.id in selected:
                continue
            if (
                event.correlation_id in correlations
                or event.causation_id in selected
                or str(event.id) in {
                    str(candidate.causation_id)
                    for candidate in events
                    if candidate.id in selected and candidate.causation_id
                }
            ):
                selected.add(event.id)
                if event.correlation_id is not None:
                    correlations.add(event.correlation_id)
                changed = True
    return [event for event in events if event.id in selected]


def _reviews(
    continuations,
    *,
    process_by_id,
    project_process_ids,
    related_ids,
    event_ids,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for continuation in continuations:
        conditions = continuation.waiting_for.get("any") or [continuation.waiting_for]
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            event_type = str(condition.get("event_type") or "")
            if not event_type.endswith("_reviewed"):
                continue
            process = process_by_id.get(continuation.process_instance_id)
            values = _reference_values(condition) | _reference_values(
                continuation.saved_process_state
            )
            if (
                continuation.process_instance_id not in project_process_ids
                and not values & related_ids
                and not values & event_ids
            ):
                continue
            identifiers = [
                str(value) for key, value in condition.items() if key.endswith("_id")
            ]
            result.append(
                {
                    "id": identifiers[0] if identifiers else str(continuation.id),
                    "continuation_id": str(continuation.id),
                    "event_type": event_type,
                    "process": process.definition_name if process else None,
                    "condition": _json_safe(condition),
                    "created_at": continuation.created_at.isoformat(),
                }
            )
    return sorted(result, key=lambda item: (item["created_at"], item["id"]))


def _questions(state_entries, project_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in state_entries:
        values: Iterable[Any] = ()
        if entry.entity == "self" and entry.attribute == "unresolved_questions":
            values = _items(entry.value)
        elif (
            entry.entity == f"project:{project_id}"
            and entry.attribute == "unresolved_questions"
        ):
            values = _items(entry.value)
        else:
            continue
        for value in values:
            if entry.entity == "self":
                explicit = _association(value)[0] if isinstance(value, dict) else None
                if explicit != project_id:
                    continue
            result.append(
                {
                    "id": self_question_id(value),
                    "text": _question_text(value),
                    "value": _json_safe(value),
                    "updated_at": entry.updated_at.isoformat(),
                }
            )
    return result


def _blockers(*, goals, intentions, works, blocked_work, processes, reviews):
    blockers: list[dict[str, Any]] = []
    for goal in goals:
        if goal.status is GoalStatus.BLOCKED:
            blockers.append(
                {
                    "type": "BLOCKED_GOAL",
                    "severity": "blocked",
                    "goal_id": str(goal.id),
                    "summary": goal.title,
                }
            )
    for intention in intentions:
        if intention.status is IntentionStatus.BLOCKED:
            blockers.append(
                {
                    "type": "BLOCKED_INTENTION",
                    "severity": "blocked",
                    "intention_id": str(intention.id),
                    "goal_id": str(intention.goal_id),
                    "summary": intention.reason or intention.focus,
                }
            )
    for work in blocked_work:
        blockers.append(
            {
                "type": work.status.value,
                "severity": "blocked",
                "work_id": str(work.id),
                "summary": work.objective or work.work_type,
                "missing_capabilities": list(work.missing_capabilities),
            }
        )
    work_by_id = {work.id: work for work in works}
    by_identity = {
        **{str(work.id): work for work in works},
        **{work.work_key: work for work in works},
    }
    for work in works:
        if work.status in {WorkStatus.SATISFIED, WorkStatus.CANCELLED}:
            continue
        for dependency in _dependency_values(work.metadata.get("depends_on")):
            target = by_identity.get(dependency)
            if target is None or target.status is not WorkStatus.SATISFIED:
                blockers.append(
                    {
                        "type": "UNRESOLVED_DEPENDENCY",
                        "severity": "blocked",
                        "work_id": str(work.id),
                        "dependency": dependency,
                        "summary": f"{work.objective or work.work_type} waits for {dependency}",
                    }
                )
    for process in processes:
        if process.status is not ProcessStatus.FAILED:
            continue
        work = work_by_id.get(process.work_requirement_id)
        if work is None or work.status in {WorkStatus.SATISFIED, WorkStatus.CANCELLED}:
            continue
        blockers.append(
            {
                "type": "FAILED_WORK",
                "severity": "blocked",
                "work_id": str(work.id),
                "process_id": str(process.id),
                "summary": process.last_error or f"{work.objective or work.work_type} failed",
            }
        )
    for review in reviews:
        blockers.append(
            {
                "type": "PENDING_REVIEW",
                "severity": "attention",
                "review_id": review["id"],
                "summary": f"{review['event_type']} requires a human decision",
            }
        )
    return blockers


def _recent_changes(events, history, works, limit: int):
    changes: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        changes.append(
            (
                event.occurred_at,
                {
                    "type": "EVENT",
                    "id": str(event.id),
                    "summary": event.type.replace("_", " "),
                    "occurred_at": event.occurred_at.isoformat(),
                },
            )
        )
    for entry in history:
        changes.append(
            (
                entry.created_at,
                {
                    "type": "WORLD_STATE",
                    "id": str(entry.id),
                    "summary": f"{entry.entity}.{entry.attribute} updated",
                    "entity": entry.entity,
                    "attribute": entry.attribute,
                    "value": _json_safe(entry.value),
                    "version": entry.version,
                    "source_event_id": (
                        str(entry.source_event) if entry.source_event else None
                    ),
                    "occurred_at": entry.created_at.isoformat(),
                },
            )
        )
    for work in works:
        changes.append(
            (
                work.updated_at,
                {
                    "type": "WORK_STATUS",
                    "id": str(work.id),
                    "summary": f"{work.objective or work.work_type}: {work.status.value}",
                    "status": work.status.value,
                    "occurred_at": work.updated_at.isoformat(),
                },
            )
        )
    changes.sort(key=lambda item: (item[0], item[1]["type"], item[1]["id"]), reverse=True)
    return [item for _, item in changes[:limit]]


def _association(*containers: Any) -> tuple[str | None, dict[str, Any]]:
    descriptor: dict[str, Any] = {}
    for container in containers:
        if not isinstance(container, dict):
            continue
        project = container.get("project")
        nested = project if isinstance(project, dict) else {}
        project_id = _identifier(
            container.get("project_id")
            or nested.get("project_id")
            or nested.get("id")
            or (project if isinstance(project, str) else None)
        )
        for key, candidates in {
            "title": (container.get("project_title"), nested.get("title")),
            "objective": (container.get("project_objective"), nested.get("objective")),
        }.items():
            value = next((value for value in candidates if value), None)
            if value:
                descriptor[key] = str(value)
        if project_id is not None:
            return project_id, descriptor
    return None, descriptor


def _association_from_value(value: Any) -> str | None:
    if isinstance(value, dict):
        return _association(value)[0]
    return None


def _identifier(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _reference_values(value: Any) -> set[str]:
    if isinstance(value, dict):
        result: set[str] = set()
        for key, child in value.items():
            if key.endswith("_id") or key.endswith("_ids"):
                result.update(_scalar_values(child))
        return result
    return set()


def _scalar_values(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value if item is not None}
    return {str(value)} if value is not None else set()


def _dependency_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _goal(goal) -> dict[str, Any]:
    return {
        "id": str(goal.id),
        "title": goal.title,
        "objective": goal.objective,
        "status": goal.status.value,
        "priority": goal.priority.value,
        "deadline": goal.deadline.isoformat() if goal.deadline else None,
        "updated_at": goal.updated_at.isoformat(),
    }


def _intention(intention) -> dict[str, Any]:
    return {
        "id": str(intention.id),
        "goal_id": str(intention.goal_id),
        "focus": intention.focus,
        "status": intention.status.value,
        "reason": intention.reason,
        "reconsider_on": list(intention.reconsider_on),
        "updated_at": intention.updated_at.isoformat(),
    }


def _work(work) -> dict[str, Any]:
    return {
        "id": str(work.id),
        "goal_id": str(work.goal_id) if work.goal_id else None,
        "type": work.work_type,
        "objective": work.objective,
        "status": work.status.value,
        "reason": work.reason,
        "priority": work.human_priority or work.priority,
        "missing_capabilities": list(work.missing_capabilities),
        "updated_at": work.updated_at.isoformat(),
    }


def _event(event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "type": event.type,
        "source": event.source,
        "payload": _json_safe(event.payload),
        "occurred_at": event.occurred_at.isoformat(),
        "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        "causation_id": str(event.causation_id) if event.causation_id else None,
    }


def _question_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("question") or value.get("text") or value)
    return str(value)


def _summary(title, status, *, active_goals, blocked_work, reviews, latest_change):
    label = status.value.lower().replace("_", " ")
    sentences = [f"{title} is {label}."]
    if blocked_work:
        sentences.append(f"{blocked_work} work item{'s are' if blocked_work != 1 else ' is'} blocked.")
    if active_goals:
        sentences.append(f"{active_goals} goal{'s are' if active_goals != 1 else ' is'} active.")
    if reviews:
        sentences.append(f"{reviews} review{'s require' if reviews != 1 else ' requires'} attention.")
    if latest_change:
        sentences.append(f"Latest change: {latest_change}.")
    return " ".join(sentences)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(child) for child in value]
    return value


__all__ = [
    "ACTIVE_WORK",
    "BLOCKED_WORK",
    "get_project_situation",
    "get_project_situations",
    "get_project_summaries",
    "project_status",
]
