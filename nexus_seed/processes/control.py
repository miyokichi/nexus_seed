"""Phase 5G event-driven Goal evaluation and idempotent Work discovery."""

from __future__ import annotations

import hashlib
import json
import uuid

from ..capabilities.models import CapabilityRequirement
from ..control.models import GoalStatus
from ..core.event import utcnow
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..work.work_requirement import WorkRequirement, WorkStatus


EVALUATE_GOAL = ProcessDefinition(
    name="evaluate_goal",
    version="1",
    handler="evaluate_goal",
    trigger_event_types=(
        "goal_created",
        "goal_evaluation_requested",
        "state_changed",
        "work_satisfied",
        "work_failed",
    ),
    metadata={"role": "goal_evaluator"},
)

REVIEW_HUMAN_WORK = ProcessDefinition(
    name="review_human_work",
    version="1",
    handler="review_human_work",
    trigger_event_types=("work_review_required",),
    metadata={"role": "human_work_review"},
)


async def evaluate_goal(ctx: ProcessContext) -> ProcessResult:
    """Evaluate structured criteria and emit missing Work, never an Action."""

    assert ctx.event is not None and ctx.services is not None
    requested_id = _uuid(ctx.event.payload.get("goal_id"))
    if requested_id:
        goal = ctx.services.get_goal(requested_id)
        goals = [goal] if goal is not None else []
    else:
        goals = ctx.services.get_active_goals()
    emitted = []
    generated = []
    achieved = []
    blocked = []
    for goal in goals:
        if goal.status is not GoalStatus.ACTIVE:
            continue
        if goal.deadline is not None and goal.deadline <= utcnow():
            ctx.update_goal(goal.id, GoalStatus.BLOCKED)
            blocked.append(str(goal.id))
            emitted.append(ctx.new_event("goal_blocked", {"goal_id": str(goal.id), "reason": "deadline passed"}))
            continue
        criteria = list(goal.success_criteria) or [
            {
                "type": "required_work",
                "semantic_key": "objective",
                "objective": goal.objective,
                "work_type": goal.metadata.get("work_type", "human_goal_work"),
                "required_capabilities": goal.metadata.get("required_capabilities", ["advance_human_goal"]),
            }
        ]
        all_satisfied = True
        for criterion in criteria:
            satisfied, template = _criterion(ctx, goal, criterion)
            if satisfied:
                continue
            all_satisfied = False
            if template is None:
                continue
            work_key = _goal_work_key(goal.id, template)
            if ctx.services.get_work_requirement_by_key(work_key) is not None:
                continue
            requirement = _work_for(goal, template, work_key)
            ctx.add_work_requirement(requirement)
            generated.append(str(requirement.id))
            emitted.append(ctx.new_event(
                "goal_work_generated",
                {"goal_id": str(goal.id), "work_requirement_id": str(requirement.id)},
            ))
            emitted.append(ctx.new_event(
                "work_review_required" if requirement.status is WorkStatus.WAITING_REVIEW else "work_required",
                {"work_requirement_id": str(requirement.id)},
            ))
        if all_satisfied:
            ctx.update_goal(goal.id, GoalStatus.ACHIEVED)
            achieved.append(str(goal.id))
            emitted.append(ctx.new_event("goal_achieved", {"goal_id": str(goal.id)}))
    return ctx.complete(
        output={"generated_work": generated, "achieved_goals": achieved, "blocked_goals": blocked},
        emitted_events=emitted,
    )


async def review_human_work(ctx: ProcessContext) -> ProcessResult:
    """Enforce a normal Event + Continuation before constrained Work starts."""

    assert ctx.event is not None and ctx.services is not None
    work_id = _uuid(
        ctx.saved_process_state.get("work_requirement_id")
        or ctx.event.payload.get("work_requirement_id")
    )
    work = ctx.services.get_work_requirement(work_id) if work_id else None
    if work is None:
        return ctx.fail(f"reviewed work {work_id} not found")
    if ctx.resume_point is None:
        if work.status is not WorkStatus.WAITING_REVIEW:
            return ctx.complete(output={"waiting": False, "status": work.status.value})
        return ctx.suspend(
            resume_point="await_work_review",
            waiting_for={"event_type": "work_reviewed", "work_requirement_id": str(work.id)},
            saved_process_state={"work_requirement_id": str(work.id)},
        )
    decision = str(ctx.event.payload.get("decision", "reject")).lower()
    if decision != "approve":
        ctx.mark_work(work.id, WorkStatus.CANCELLED)
        return ctx.complete(
            output={"approved": False},
            emitted_events=[ctx.new_event("work_cancelled", {
                "work_requirement_id": str(work.id), "reason": "human review rejected"
            })],
        )
    ctx.mark_work(work.id, WorkStatus.EXPECTED)
    return ctx.complete(
        output={"approved": True},
        emitted_events=[ctx.new_event("work_required", {"work_requirement_id": str(work.id)})],
    )


def _criterion(ctx, goal, criterion) -> tuple[bool, dict | None]:
    if isinstance(criterion, str):
        entry = ctx.services.get_current_state(f"goal:{goal.id}", criterion)
        return bool(entry and entry.value is True), None
    if not isinstance(criterion, dict):
        return False, None
    kind = str(criterion.get("type") or "required_work")
    if kind == "state_predicate":
        entry = ctx.services.get_current_state(str(criterion.get("entity")), str(criterion.get("attribute")))
        satisfied = bool(entry and _compare(entry.value, criterion.get("operator", "=="), criterion.get("value")))
        return satisfied, criterion.get("work") if isinstance(criterion.get("work"), dict) else None
    if kind == "artifact_available":
        resource = ctx.services.get_resource_by_uri(str(criterion.get("uri")))
        return resource is not None, criterion.get("work") if isinstance(criterion.get("work"), dict) else None
    template = criterion.get("work") if isinstance(criterion.get("work"), dict) else criterion
    work_key = _goal_work_key(goal.id, template)
    work = ctx.services.get_work_requirement_by_key(work_key)
    return bool(work and work.status is WorkStatus.SATISFIED), template


def _work_for(goal, template: dict, work_key: str) -> WorkRequirement:
    objective = str(template.get("objective") or goal.objective)
    capabilities = template.get("required_capabilities") or ["advance_human_goal"]
    scope = template.get("scope") if isinstance(template.get("scope"), dict) else goal.scope
    entities = scope.get("entities") or []
    requirement = WorkRequirement(
        work_type=str(template.get("work_type") or "human_goal_work"),
        work_key=work_key,
        related_entities=list(entities),
        reason=f"Goal gap: {objective}",
        priority=goal.priority.scheduling_value,
        metadata={"source": "goal_evaluation", "semantic_key": template.get("semantic_key")},
        required_capabilities=[CapabilityRequirement(str(name)) for name in capabilities],
        available_input_types=list(template.get("available_input_types") or ()),
        required_output_types=list(template.get("required_output_types") or ()),
        objective=objective,
        scope=dict(scope),
        human_priority=goal.priority.value,
        deadline=goal.deadline,
        constraints=goal.constraints.to_dict(),
        completion_criteria=list(template.get("completion_criteria") or ()),
        goal_id=goal.id,
    )
    if goal.constraints.human_review_required:
        requirement.status = WorkStatus.WAITING_REVIEW
    return requirement


def _goal_work_key(goal_id: uuid.UUID, template: dict) -> str:
    semantic = template.get("semantic_key") or {
        "objective": template.get("objective"),
        "scope": template.get("scope"),
        "work_type": template.get("work_type"),
        "required_capabilities": template.get("required_capabilities"),
    }
    raw = json.dumps(semantic, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"goal:{goal_id}:{digest}"


def _compare(actual, operator: str, expected) -> bool:
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    return False


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def bootstrap_control(runtime) -> None:
    """Register the ordinary Goal evaluator Process."""

    runtime.register_process(EVALUATE_GOAL, evaluate_goal)
    runtime.register_process(REVIEW_HUMAN_WORK, review_human_work)


__all__ = [
    "EVALUATE_GOAL",
    "REVIEW_HUMAN_WORK",
    "bootstrap_control",
    "evaluate_goal",
    "review_human_work",
]
