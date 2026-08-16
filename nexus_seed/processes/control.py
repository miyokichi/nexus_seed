"""Phase 5G event-driven Goal evaluation and idempotent Work discovery."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from ..backends.base import BackendRequest, LLMInvocation
from ..capabilities.models import CapabilityRequirement
from ..control.models import GoalStatus
from ..core.event import utcnow
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..presence.models import intention_id_for_goal
from ..work.work_requirement import WorkRequirement, WorkStatus


GOAL_DECOMPOSITION_PROPOSED = "goal_decomposition_proposed"
GOAL_DECOMPOSITION_FAILED = "goal_decomposition_failed"
RESERVED_INTERNAL_CAPABILITIES = frozenset({"advance_human_goal"})
_SYMBOL = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
GOAL_DECOMPOSITION_SCHEMA = {
    "type": "object",
    "required": ["work"],
    "properties": {
        "work": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": [
                    "semantic_key",
                    "objective",
                    "work_type",
                    "required_capabilities",
                ],
                "properties": {
                    "semantic_key": {"type": "string"},
                    "objective": {"type": "string"},
                    "work_type": {"type": "string"},
                    "required_capabilities": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "version_constraint": {"type": ["string", "null"]},
                                "metadata": {"type": "object"},
                            },
                        },
                    },
                    "available_input_types": {"type": "array", "items": {"type": "string"}},
                    "required_output_types": {"type": "array", "items": {"type": "string"}},
                    "completion_criteria": {"type": "array"},
                },
            },
        }
    },
}
GOAL_DECOMPOSITION_INSTRUCTION = """Decompose one durable human Goal into concrete, bounded WorkRequirements.
Return only the structured object required by the schema. Each required capability must name a
specific competence needed to perform that work (for example analyze_measurements or
generate_review_report), never a generic lifecycle phrase such as advance_human_goal,
fulfill_goal, manage_intention, or decompose_goal. Do not propose Actions, providers,
permissions, Core changes, or capability acquisition. Capability matching and acquisition
policy run later, after this proposal is validated. Prefer existing capabilities when they
actually satisfy the work; otherwise name the precise missing competence truthfully."""


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
        GOAL_DECOMPOSITION_PROPOSED,
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
        if not goal.success_criteria and _phase6_intention_path_enabled(ctx):
            decomposition = await _evaluate_decomposed_goal(ctx, goal)
            if isinstance(decomposition, ProcessResult):
                return decomposition
            decomposition_events, decomposition_work, decomposition_achieved = decomposition
            emitted.extend(decomposition_events)
            generated.extend(decomposition_work)
            if decomposition_achieved:
                ctx.update_goal(goal.id, GoalStatus.ACHIEVED)
                achieved.append(str(goal.id))
                emitted.append(ctx.new_event("goal_achieved", {"goal_id": str(goal.id)}))
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


async def _evaluate_decomposed_goal(
    ctx: ProcessContext, goal
) -> tuple[list, list[str], bool] | ProcessResult:
    """Use the existing Goal evaluator as the Phase 6 decomposition boundary.

    The LLM may only emit a durable, inert proposal Event. A later activation
    validates that Event again before creating ordinary WorkRequirements.
    """

    assert ctx.event is not None and ctx.services is not None
    existing = [
        work
        for work in ctx.services.get_work_for_goal(goal.id)
        if work.metadata.get("source") == "goal_decomposition"
    ]
    if existing:
        return [], [], all(work.status is WorkStatus.SATISFIED for work in existing)

    if ctx.event.type == GOAL_DECOMPOSITION_PROPOSED:
        try:
            templates = _validate_goal_decomposition(ctx.event.payload.get("work"))
        except ValueError as exc:
            return ctx.complete(
                output={"decomposed": False, "reason": str(exc)},
                emitted_events=[ctx.new_event(
                    GOAL_DECOMPOSITION_FAILED,
                    {"goal_id": str(goal.id), "reason": str(exc), "proposal_event_id": str(ctx.event.id)},
                )],
            )
        emitted = []
        generated = []
        for template in templates:
            template["_source"] = "goal_decomposition"
            template["_proposal_event_id"] = str(ctx.event.id)
            work_key = _goal_work_key(goal.id, template)
            if ctx.services.get_work_requirement_by_key(work_key) is not None:
                continue
            requirement = _work_for(goal, template, work_key)
            ctx.add_work_requirement(requirement)
            generated.append(str(requirement.id))
            emitted.extend([
                ctx.new_event(
                    "goal_work_generated",
                    {"goal_id": str(goal.id), "work_requirement_id": str(requirement.id)},
                ),
                ctx.new_event(
                    "work_review_required"
                    if requirement.status is WorkStatus.WAITING_REVIEW
                    else "work_required",
                    {"work_requirement_id": str(requirement.id)},
                ),
            ])
        return emitted, generated, False

    # On goal_created, evaluate_goal and maintain_intention are both routed.
    # Wait for the durable Intention rather than racing it and manufacturing a
    # generic capability gap. The Intention Process emits a targeted
    # goal_evaluation_requested Event once its StateDelta is applied.
    intention = ctx.services.get_current_state(
        f"intention:{intention_id_for_goal(goal.id)}", "record"
    )
    if intention is None and not ctx.event.payload.get("intention_id"):
        return [], [], False

    deterministic = _metadata_decomposition(goal)
    if deterministic is not None:
        return [ctx.new_event(
            GOAL_DECOMPOSITION_PROPOSED,
            {"goal_id": str(goal.id), "work": deterministic, "source": "goal_metadata"},
        )], [], False
    return await _propose_goal_decomposition(ctx, goal)


def _phase6_intention_path_enabled(ctx: ProcessContext) -> bool:
    """Detect the feature-gated Phase 6 composition through its definition."""

    return bool(
        ctx.services
        and ctx.services.get_definition("maintain_intention", "1") is not None
    )


def _metadata_decomposition(goal) -> list[dict] | None:
    """Use an explicit Goal capability declaration without asking an LLM."""

    capabilities = goal.metadata.get("required_capabilities")
    if not capabilities:
        return None
    return _validate_goal_decomposition([
        {
            "semantic_key": goal.metadata.get("semantic_key", "objective"),
            "objective": goal.objective,
            "work_type": goal.metadata.get("work_type", "human_goal_work"),
            "required_capabilities": capabilities,
            "available_input_types": goal.metadata.get("available_input_types", []),
            "required_output_types": goal.metadata.get("required_output_types", []),
            "completion_criteria": goal.metadata.get("completion_criteria", []),
        }
    ])


async def _propose_goal_decomposition(
    ctx: ProcessContext, goal
) -> tuple[list, list[str], bool] | ProcessResult:
    """Ask the configured reasoning backend for an inert decomposition proposal."""

    backend = ctx.backends.get("llm") if ctx.backends else None
    if backend is None:
        ctx.logger.info(
            "goal %s awaits explicit success criteria: no LLM decomposition backend",
            goal.id,
        )
        return [], [], False
    registry = ctx.services.get_capability_registry()
    available = sorted(registry.provided_capability_names()) if registry else []
    request = BackendRequest(
        instruction=GOAL_DECOMPOSITION_INSTRUCTION,
        context={
            "goal": {
                "id": str(goal.id),
                "title": goal.title,
                "objective": goal.objective,
                "scope": goal.scope,
                "constraints": goal.constraints.to_dict(),
                "priority": goal.priority.value,
            },
            "currently_available_capabilities": available,
        },
        output_schema=GOAL_DECOMPOSITION_SCHEMA,
        metadata={"purpose": "goal_decomposition", "goal_id": str(goal.id)},
    )
    result = await backend.execute(request)
    invocation = LLMInvocation(
        process_instance_id=ctx.instance.id,
        backend="llm",
        activation_id=ctx.activation_id,
        model=result.model,
        request_metadata={"purpose": "goal_decomposition", "goal_id": str(goal.id)},
        response_metadata={"usage": result.usage, "latency_ms": result.latency_ms},
        context_snapshot_id=ctx.context_snapshot_id,
        success=result.success,
        error=result.error,
    )
    ctx.record_llm_invocation(invocation)
    problem = None
    templates = None
    if not result.success:
        problem = f"backend failure: {result.error}"
    elif not isinstance(result.parsed_output, dict):
        problem = "backend returned no structured decomposition object"
    else:
        try:
            templates = _validate_goal_decomposition(result.parsed_output.get("work"))
        except ValueError as exc:
            problem = f"schema validation failed: {exc}"
    if problem is not None:
        invocation.success = False
        invocation.error = problem
        if ctx.instance.retry_count < ctx.instance.max_retries:
            return ctx.retry(problem)
        return ctx.complete(
            output={"decomposed": False, "reason": problem},
            emitted_events=[ctx.new_event(
                GOAL_DECOMPOSITION_FAILED,
                {"goal_id": str(goal.id), "reason": problem, "llm_invocation_id": str(invocation.id)},
            )],
        )
    return [ctx.new_event(
        GOAL_DECOMPOSITION_PROPOSED,
        {
            "goal_id": str(goal.id),
            "work": templates,
            "source": "llm",
            "llm_invocation_id": str(invocation.id),
        },
    )], [], False


def _validate_goal_decomposition(raw) -> list[dict]:
    """Validate and normalize an untrusted Goal decomposition proposal."""

    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError("work must contain between 1 and 8 items")
    normalized = []
    semantic_keys: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"work[{index}] must be an object")
        semantic_key = str(item.get("semantic_key") or "").strip()
        objective = str(item.get("objective") or "").strip()
        work_type = str(item.get("work_type") or "").strip()
        if not _SYMBOL.fullmatch(semantic_key):
            raise ValueError(f"work[{index}].semantic_key must be a bounded symbolic name")
        if semantic_key in semantic_keys:
            raise ValueError(f"duplicate semantic_key {semantic_key!r}")
        if not objective or len(objective) > 1000:
            raise ValueError(f"work[{index}].objective must contain 1 to 1000 characters")
        if not _SYMBOL.fullmatch(work_type):
            raise ValueError(f"work[{index}].work_type must be a bounded symbolic name")
        raw_capabilities = item.get("required_capabilities")
        if not isinstance(raw_capabilities, list) or not 1 <= len(raw_capabilities) <= 8:
            raise ValueError(f"work[{index}].required_capabilities must contain 1 to 8 items")
        capabilities = []
        names: set[str] = set()
        for raw_capability in raw_capabilities:
            if not isinstance(raw_capability, (str, dict, CapabilityRequirement)):
                raise ValueError(f"work[{index}] contains an invalid capability object")
            capability = CapabilityRequirement.coerce(raw_capability)
            if not _SYMBOL.fullmatch(capability.name):
                raise ValueError(f"work[{index}] contains an invalid capability name")
            if capability.name in RESERVED_INTERNAL_CAPABILITIES:
                raise ValueError(
                    f"{capability.name} is an internal lifecycle placeholder, not acquirable work"
                )
            if capability.name in names:
                raise ValueError(f"work[{index}] repeats capability {capability.name!r}")
            if not isinstance(capability.metadata, dict):
                raise ValueError(f"work[{index}] capability metadata must be an object")
            if len(json.dumps(capability.metadata, ensure_ascii=False)) > 16_384:
                raise ValueError(f"work[{index}] capability metadata is too large")
            names.add(capability.name)
            capabilities.append(capability.to_dict())
        semantic_keys.add(semantic_key)
        normalized.append({
            "semantic_key": semantic_key,
            "objective": objective,
            "work_type": work_type,
            "required_capabilities": capabilities,
            "available_input_types": _bounded_symbols(
                item.get("available_input_types"), f"work[{index}].available_input_types"
            ),
            "required_output_types": _bounded_symbols(
                item.get("required_output_types"), f"work[{index}].required_output_types"
            ),
            "completion_criteria": _bounded_json_list(
                item.get("completion_criteria"), f"work[{index}].completion_criteria", 8
            ),
        })
    return normalized


def _bounded_symbols(raw, field: str) -> list[str]:
    values = raw or []
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError(f"{field} must be an array with at most 16 items")
    result = [str(value).strip() for value in values]
    if any(not value or len(value) > 128 for value in result):
        raise ValueError(f"{field} contains an invalid symbolic value")
    return result


def _bounded_json_list(raw, field: str, maximum: int) -> list:
    values = raw or []
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError(f"{field} must be an array with at most {maximum} items")
    if len(json.dumps(values, ensure_ascii=False)) > 32_768:
        raise ValueError(f"{field} is too large")
    return list(values)


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
        metadata={
            "source": template.get("_source", "goal_evaluation"),
            "semantic_key": template.get("semantic_key"),
            "goal_decomposition_proposal_event_id": template.get("_proposal_event_id"),
        },
        required_capabilities=[CapabilityRequirement.coerce(value) for value in capabilities],
        available_input_types=list(template.get("available_input_types") or ()),
        required_output_types=list(template.get("required_output_types") or ()),
        objective=objective,
        scope=dict(scope),
        # Work generated for a Goal belongs to that Goal's project.  Recording it
        # on the Work itself keeps the membership explicit for replanned and
        # restarted Work rather than leaving it to be re-derived (Invariant 197).
        project=goal.metadata.get("project_id"),
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
    "GOAL_DECOMPOSITION_FAILED",
    "GOAL_DECOMPOSITION_PROPOSED",
    "REVIEW_HUMAN_WORK",
    "bootstrap_control",
    "evaluate_goal",
    "review_human_work",
]
