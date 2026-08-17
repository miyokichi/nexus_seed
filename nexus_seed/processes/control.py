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

#: Emitted by :class:`~nexus_seed.providers.registry.ProviderRegistry` when an
#: external delegation completes.  Goal decomposition listens for the subset of
#: those that were its own request, and ignores every other one.
PROVIDER_EXECUTION_COMPLETED = "provider_execution_completed"
#: The competence that proposes what Work a Goal still needs.  When some
#: process provides it *and* has an eligible execution provider, decomposition
#: goes through the ordinary Work path instead of calling an LLM directly.
WORK_GENERATION_CAPABILITY = "work_generation"
#: The typed output that competence returns.
WORK_CANDIDATE_OUTPUT_TYPE = "work_candidate"
#: ``WorkRequirement.metadata["source"]`` marking the delegation request
#: itself, kept distinct from ``goal_decomposition`` (the Work it produces).
GOAL_DECOMPOSITION_REQUEST_SOURCE = "goal_decomposition_request"
#: Stable semantic key of that request, so its ``work_key`` is derived once per
#: Goal and re-evaluation finds the existing row instead of making another.
GOAL_DECOMPOSITION_REQUEST_KEY = "goal_decomposition_request"
GOAL_DECOMPOSITION_WORK_TYPE = "goal_decomposition"
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
        PROVIDER_EXECUTION_COMPLETED,
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
    if ctx.event.type == PROVIDER_EXECUTION_COMPLETED:
        # Most external delegations have nothing to do with a Goal.  Answering
        # only for our own request keeps this Process out of everyone else's
        # provider lifecycle.
        return _decomposition_from_provider(ctx)
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
    delegated = _delegate_goal_decomposition(ctx, goal)
    if delegated is not None:
        return delegated
    return await _propose_goal_decomposition(ctx, goal)


def _delegate_goal_decomposition(
    ctx: ProcessContext, goal
) -> tuple[list, list[str], bool] | None:
    """Ask for decomposition as ordinary Work when a competence can do it.

    This adds no execution path of its own.  It states a need — one
    WorkRequirement wanting ``work_generation`` — and lets the existing
    matcher, spawner, ProviderSelector and ProviderRegistry decide where that
    need is met.  ``None`` means no such competence is currently usable, and is
    the *only* condition under which the caller falls back to calling an LLM
    backend directly.
    """

    assert ctx.services is not None
    template = _decomposition_request_template(goal)
    work_key = _goal_work_key(goal.id, template)
    if ctx.services.get_work_requirement_by_key(work_key) is not None:
        # The delegation was already requested.  Whatever became of it —
        # running, satisfied, or failed at the provider — it is that request's
        # outcome that stands.  Re-deciding here would either duplicate the
        # Work or quietly re-do the same decomposition through a different
        # executor after execution had already started (Invariant: no silent
        # failover once a provider was selected).
        return [], [], False
    if not _work_generation_available(ctx):
        return None

    requirement = _work_for(goal, template, work_key)
    ctx.add_work_requirement(requirement)
    return [
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
    ], [str(requirement.id)], False


def _decomposition_request_template(goal) -> dict:
    """The Work that asks for this Goal's decomposition, as a normal template."""

    return {
        "semantic_key": GOAL_DECOMPOSITION_REQUEST_KEY,
        "objective": (
            "Propose the Work this Goal still needs, as bounded work candidates. "
            f"Goal: {goal.objective}"
        ),
        "work_type": GOAL_DECOMPOSITION_WORK_TYPE,
        "required_capabilities": [WORK_GENERATION_CAPABILITY],
        "required_output_types": [WORK_CANDIDATE_OUTPUT_TYPE],
        "_source": GOAL_DECOMPOSITION_REQUEST_SOURCE,
    }


def _work_generation_available(ctx: ProcessContext) -> bool:
    """Whether some enabled process can *actually run* ``work_generation`` now.

    Deliberately :meth:`CapabilityMatcher.provides`, which already answers
    "capable process, with an eligible execution provider" — the same question
    ``work_matcher`` will ask a moment later, so the two cannot disagree.
    """

    matcher = ctx.services.get_capability_matcher() if ctx.services else None
    if matcher is None:
        return False
    return matcher.provides(
        CapabilityRequirement(WORK_GENERATION_CAPABILITY),
        ctx.services.get_all_definitions(),
    )


def _decomposition_from_provider(ctx: ProcessContext) -> ProcessResult:
    """Turn one completed ``work_generation`` delegation into a proposal.

    The provider's typed output is translated into the *existing* proposal
    shape and re-enters the ordinary path: validation, idempotency and Goal
    association all stay where they already were.
    """

    assert ctx.event is not None and ctx.services is not None
    invocation = _provider_invocation(ctx, ctx.event.payload.get("provider_invocation_id"))
    requirement = _decomposition_request_for(ctx, invocation)
    if requirement is None:
        return ctx.complete(output={"goal_decomposition": False})
    goal = ctx.services.get_goal(requirement.goal_id) if requirement.goal_id else None
    if goal is None:
        return ctx.complete(
            output={"goal_decomposition": False, "reason": "goal no longer exists"}
        )

    snapshot = invocation.result_snapshot or {}
    try:
        templates = _work_candidate_templates(snapshot.get("typed_outputs") or [])
    except ValueError as exc:
        # A provider that answered the wrong shape is a provider failure, not a
        # reason to decompose the Goal some other way.
        return ctx.complete(
            output={"goal_decomposition": True, "decomposed": False, "reason": str(exc)},
            emitted_events=[ctx.new_event(
                GOAL_DECOMPOSITION_FAILED,
                {
                    "goal_id": str(goal.id),
                    "reason": str(exc),
                    "provider_invocation_id": str(invocation.id),
                    "work_requirement_id": str(requirement.id),
                },
            )],
        )
    return ctx.complete(
        output={"goal_decomposition": True, "work_candidates": len(templates)},
        emitted_events=[ctx.new_event(
            GOAL_DECOMPOSITION_PROPOSED,
            {
                "goal_id": str(goal.id),
                "work": templates,
                "source": "provider",
                "provider_invocation_id": str(invocation.id),
            },
        )],
    )


def _provider_invocation(ctx: ProcessContext, raw_id):
    """Read one delegation journal record through the existing registry."""

    registry = ctx.services.get_provider_registry() if ctx.services else None
    invocation_id = _uuid(raw_id)
    if registry is None or invocation_id is None:
        return None
    return registry.store.get_invocation(invocation_id)


def _decomposition_request_for(ctx: ProcessContext, invocation):
    """Return the decomposition request this invocation ran, or ``None``.

    Both halves matter: the Work has to be one this Process asked for, *and*
    the delegation has to have been for ``work_generation``.
    """

    if invocation is None:
        return None
    instance = ctx.services.get_process_instance(invocation.process_instance_id)
    if instance is None or instance.work_requirement_id is None:
        return None
    requirement = ctx.services.get_work_requirement(instance.work_requirement_id)
    if requirement is None:
        return None
    if requirement.metadata.get("source") != GOAL_DECOMPOSITION_REQUEST_SOURCE:
        return None
    if not any(
        item.name == WORK_GENERATION_CAPABILITY
        for item in requirement.required_capabilities
    ):
        return None
    return requirement


def _work_candidate_templates(typed_outputs) -> list[dict]:
    """Map ``work_candidate`` outputs onto the existing proposal templates.

    The Skill's contract names the same things by different words; only that
    renaming happens here.  The result is handed to the existing
    :func:`_validate_goal_decomposition`, which remains the single place that
    decides whether an untrusted decomposition is acceptable.
    """

    candidates = []
    for item in typed_outputs:
        if not isinstance(item, dict) or str(item.get("type")) != WORK_CANDIDATE_OUTPUT_TYPE:
            continue
        value = item.get("value")
        if isinstance(value, dict) and isinstance(value.get("work_candidates"), list):
            candidates.extend(value["work_candidates"])
        elif isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(value)
    if not candidates:
        raise ValueError(
            f"provider returned no {WORK_CANDIDATE_OUTPUT_TYPE} output to decompose"
        )

    templates = []
    keys: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"{WORK_CANDIDATE_OUTPUT_TYPE}[{index}] must be an object")
        work_type = str(candidate.get("work_type") or "").strip()
        templates.append({
            "semantic_key": _unique_semantic_key(
                candidate.get("semantic_key") or work_type, keys
            ),
            # ``reason`` is this Skill's word for why the Work is needed, which
            # is what a WorkRequirement records as its objective.
            "objective": str(
                candidate.get("objective") or candidate.get("reason") or ""
            ).strip(),
            "work_type": work_type,
            "required_capabilities": candidate.get("required_capabilities") or [],
            "available_input_types": candidate.get("available_input_types") or [],
            "required_output_types": candidate.get("required_output_types") or [],
            "completion_criteria": candidate.get("completion_criteria") or [],
        })
    return _validate_goal_decomposition(templates)


def _unique_semantic_key(raw, used: set[str]) -> str:
    """Keep two candidates for the same work type distinguishable.

    Only uniqueness is settled here; whether the name is acceptable at all is
    still :func:`_validate_goal_decomposition`'s decision.
    """

    base = str(raw or "").strip()
    key = base
    suffix = 2
    while key in used:
        key = f"{base}_{suffix}"
        suffix += 1
    used.add(key)
    return key


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
    "GOAL_DECOMPOSITION_REQUEST_SOURCE",
    "PROVIDER_EXECUTION_COMPLETED",
    "REVIEW_HUMAN_WORK",
    "WORK_CANDIDATE_OUTPUT_TYPE",
    "WORK_GENERATION_CAPABILITY",
    "bootstrap_control",
    "evaluate_goal",
    "review_human_work",
]
