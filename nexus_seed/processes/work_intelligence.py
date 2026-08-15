"""Work-intelligence processes: turn world changes into spawned work.

Each stage is a separate, ordinary Process (no new primitive), connected by
events, so the concerns stay cleanly separated (see spec):

    state_changed  -> impact_analysis      -> WorkRequirement + work_required
    work_required  -> work_matcher         -> work_matched (NEW / ALREADY_*)
    work_matched   -> missing_work_detector -> work_missing (only if NEW)
    work_missing   -> work_spawner          -> spawn work process + work_spawned
    (spawned)         resistance_check      -> resistance_analysis_completed
                                            -> WorkRequirement SATISFIED

The Runtime knows none of these rules — they live here and in
:mod:`nexus_seed.work.rules`.  Impact analysis never spawns directly; matching,
missing-work detection and spawning are distinct steps.
"""

from __future__ import annotations

import uuid

from ..context.requirements import (
    ContextRequirements,
    ContinuationReq,
    WorkReq,
    WorldStateReq,
)
from ..core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    SpawnSpec,
)
from ..capabilities.models import (
    CapabilityMatchStatus,
    CapabilityRef,
    CapabilityWorkMatch,
)
from ..work.rules import (
    WORK_PRIORITY,
    build_work_key,
    capabilities_for_work_type,
    expected_work_types,
    io_types_for_work_type,
    process_for_work_type,
)
from ..work.work_match import WorkMatchStatus
from ..work.work_requirement import WorkStatus

DEFAULT_WAFER = "W03"


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


# --- process definitions ---------------------------------------------------

IMPACT_ANALYSIS = ProcessDefinition(
    name="impact_analysis",
    version="1",
    handler="impact_analysis",
    trigger_event_types=("state_changed",),
    metadata={"role": "impact_analysis"},
)

WORK_MATCHER = ProcessDefinition(
    name="work_matcher",
    version="1",
    handler="work_matcher",
    trigger_event_types=("work_required",),
    metadata={"role": "work_matcher"},
)

MISSING_WORK_DETECTOR = ProcessDefinition(
    name="missing_work_detector",
    version="1",
    handler="missing_work_detector",
    trigger_event_types=("work_matched",),
    metadata={"role": "missing_work_detector"},
)

WORK_SPAWNER = ProcessDefinition(
    name="work_spawner",
    version="1",
    handler="work_spawner",
    trigger_event_types=("work_missing",),
    metadata={"role": "work_spawner"},
)

RESISTANCE_CHECK = ProcessDefinition(
    name="resistance_check",
    version="1",
    handler="resistance_check",
    metadata={"role": "work"},
    # What this process can accomplish, rather than what it is called.  Work
    # needing `analyze_resistance` finds it through the registry now; the
    # legacy work_type table still resolves to the same process (spec §55).
    provides_capabilities=(CapabilityRef("analyze_resistance"),),
    # This process's standard inputs come from the compiled Context, not from
    # direct store reads: the entities named by its WorkRequirement, that
    # requirement itself, the trigger event, and (on resume) the continuation.
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        world_state=WorldStateReq(include_work_entities=True),
        work=WorkReq(current=True),
        continuation=ContinuationReq(include=True),
    ),
)


# --- handlers --------------------------------------------------------------

async def impact_analysis(ctx: ProcessContext) -> ProcessResult:
    """Derive the work a ``state_changed`` implies (Expected Work)."""
    assert ctx.event is not None
    p = ctx.event.payload
    entity = p["entity"]
    attribute = p["attribute"]
    version = int(p.get("version", 1))
    state_delta_id = _uuid(p.get("state_delta_id"))

    emitted = []
    created = []
    for work_type in expected_work_types(entity, attribute):
        work_key = build_work_key(work_type, entity, version)
        inputs, outputs = io_types_for_work_type(work_type)
        # Idempotency: never derive the same work twice for the same version.
        if (
            ctx.services is not None
            and ctx.services.get_work_requirement_by_key(work_key) is not None
        ):
            continue
        requirement = ctx.require_work(
            work_type=work_type,
            work_key=work_key,
            related_entities=[entity],
            reason=f"{entity}.{attribute} changed to v{version}",
            source_state_delta_id=state_delta_id,
            priority=WORK_PRIORITY.get(work_type, 0),
            # What doing this work *takes*, rather than what to call it.
            required_capabilities=capabilities_for_work_type(work_type),
            # What a composed plan may start from and must end with (Phase 4B).
            available_input_types=inputs,
            required_output_types=outputs,
        )
        created.append(work_key)
        emitted.append(
            ctx.new_event("work_required", {"work_requirement_id": str(requirement.id)})
        )

    return ctx.complete(output={"expected_work": created}, emitted_events=emitted)


async def work_matcher(ctx: ProcessContext) -> ProcessResult:
    """Decide whether a requirement is doable, and whether it is already covered.

    Two questions in order (spec §28).  *Can we do this at all* comes first —
    asking "is someone already doing it?" about work the system has no
    competence for would answer the wrong question.
    """
    assert ctx.event is not None and ctx.services is not None
    requirement_id = _uuid(ctx.event.payload["work_requirement_id"])
    requirement = ctx.services.get_work_requirement(requirement_id)
    if requirement is None:
        return ctx.fail(f"work requirement {requirement_id} not found")
    if requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW, WorkStatus.CANCELLED}:
        return ctx.complete(output={"matched": False, "reason": requirement.status.value})

    if requirement.needs_capabilities:
        blocked = _match_capabilities(ctx, requirement)
        if blocked is not None:
            return blocked

    active = ctx.services.active_processes_for_work_key(requirement.work_key)
    completed = ctx.services.completed_processes_for_work_key(requirement.work_key)

    if active:
        status, process_id = WorkMatchStatus.ALREADY_RUNNING, active[0].id
        ctx.mark_work(requirement_id, WorkStatus.MATCHED)
    elif completed:
        status, process_id = WorkMatchStatus.ALREADY_COMPLETED, completed[0].id
        ctx.mark_work(requirement_id, WorkStatus.MATCHED)
    else:
        status, process_id = WorkMatchStatus.NEW, None

    matched = ctx.new_event(
        "work_matched",
        {
            "work_requirement_id": str(requirement_id),
            "match_status": status.value,
            "process_instance_id": str(process_id) if process_id else None,
        },
    )
    return ctx.complete(output={"match_status": status.value}, emitted_events=[matched])


def _match_capabilities(ctx: ProcessContext, requirement) -> ProcessResult | None:
    """Run capability matching; return a result if the work cannot proceed.

    ``None`` means "a capable process exists, carry on with the normal
    already-running / already-completed checks".
    """
    matcher = ctx.services.get_capability_matcher()
    if matcher is None:  # capability layer not wired: fall back to legacy
        return None

    result = matcher.match(
        list(requirement.required_capabilities), ctx.services.get_all_definitions()
    )
    ctx.record_capability_match(
        CapabilityWorkMatch.from_result(
            requirement.id, list(requirement.required_capabilities), result
        )
    )

    if result.eligible:
        # Record the choice only.  Where the work *stands* is decided just
        # below by the ordinary already-running / already-completed checks.
        ctx.match_work(requirement.id, selected_definition=result.selected.key)
        ctx.logger.info(
            "work %s matched capability-wise to %s v%s",
            requirement.work_key,
            *result.selected.key,
        )
        return None

    if result.status is CapabilityMatchStatus.MISSING_PROVIDER:
        ctx.match_work(
            requirement.id,
            status=WorkStatus.BLOCKED_PROVIDER,
            missing_capabilities=[],
        )
        ctx.logger.info(
            "work %s BLOCKED_PROVIDER: %s",
            requirement.work_key,
            result.reasons,
        )
        return ctx.complete(
            output={"match_status": result.status.value, "blocked": True},
            emitted_events=[
                ctx.new_event(
                    "provider_missing",
                    {
                        "work_requirement_id": str(requirement.id),
                        "work_type": requirement.work_type,
                        "required_capabilities": [
                            r.name for r in requirement.required_capabilities
                        ],
                        "reasons": list(result.reasons),
                    },
                )
            ],
        )

    # The need stands; we simply cannot do it right now (Invariant 51).
    ctx.match_work(
        requirement.id,
        status=WorkStatus.BLOCKED_CAPABILITY,
        missing_capabilities=result.missing_capabilities,
    )
    ctx.logger.info(
        "work %s BLOCKED_CAPABILITY (%s): %s",
        requirement.work_key,
        result.status.value,
        result.reasons,
    )
    emitted = [
        ctx.new_event(
            "capability_missing",
            {
                "work_requirement_id": str(requirement.id),
                "work_type": requirement.work_type,
                "match_status": result.status.value,
                "required_capabilities": [
                    r.name for r in requirement.required_capabilities
                ],
                "missing_capabilities": list(result.missing_capabilities),
                "reasons": list(result.reasons),
            },
        )
    ]
    if result.status is CapabilityMatchStatus.COMPOSITION_REQUIRED:
        # Every competence exists, just not in one process.  That is a
        # composition problem, not a gap — hand it to the planner (spec §29).
        emitted.append(
            ctx.new_event(
                "composition_required",
                {
                    "work_requirement_id": str(requirement.id),
                    "work_type": requirement.work_type,
                    "required_capabilities": [
                        r.name for r in requirement.required_capabilities
                    ],
                },
            )
        )
    return ctx.complete(
        output={"match_status": result.status.value, "blocked": True},
        emitted_events=emitted,
    )


async def missing_work_detector(ctx: ProcessContext) -> ProcessResult:
    """Decide whether a spawn is required for a matched requirement."""
    assert ctx.event is not None
    p = ctx.event.payload
    requirement = (
        ctx.services.get_work_requirement(_uuid(p.get("work_requirement_id")))
        if ctx.services is not None else None
    )
    if requirement is not None and requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW, WorkStatus.CANCELLED}:
        return ctx.complete(output={"spawn_required": False, "reason": requirement.status.value})
    requirement_id = p["work_requirement_id"]
    if p.get("match_status") == WorkMatchStatus.NEW.value:
        missing = ctx.new_event("work_missing", {"work_requirement_id": requirement_id})
        return ctx.complete(output={"spawn_required": True}, emitted_events=[missing])
    return ctx.complete(output={"spawn_required": False})


async def work_spawner(ctx: ProcessContext) -> ProcessResult:
    """Spawn the process that performs a missing work requirement."""
    assert ctx.event is not None and ctx.services is not None
    requirement_id = _uuid(ctx.event.payload["work_requirement_id"])
    requirement = ctx.services.get_work_requirement(requirement_id)
    if requirement is None:
        return ctx.fail(f"work requirement {requirement_id} not found")
    if requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW, WorkStatus.CANCELLED}:
        return ctx.complete(output={"spawned": False, "reason": requirement.status.value})

    # Capability matching already decided; spawning does not re-decide.
    # Falling back to the legacy name table keeps Phase 2C work running
    # unchanged (spec §16) — the two paths coexist during the migration.
    target = None
    if requirement.selected_definition_name:
        target = (
            requirement.selected_definition_name,
            requirement.selected_definition_version,
        )
    if target is None:
        target = process_for_work_type(requirement.work_type)
    if target is None:
        ctx.mark_work(requirement_id, WorkStatus.CANCELLED)
        return ctx.complete(
            output={"spawned": False, "reason": "no process for work_type"},
            emitted_events=[
                ctx.new_event(
                    "work_cancelled",
                    {
                        "work_requirement_id": str(requirement_id),
                        "reason": "no process for work_type",
                    },
                )
            ],
        )

    name, version = target
    entity = requirement.related_entities[0] if requirement.related_entities else None
    spec = SpawnSpec(
        definition_name=name,
        definition_version=version,
        input={
            "work_requirement_id": str(requirement.id),
            "work_key": requirement.work_key,
            "entity": entity,
            "wafer": DEFAULT_WAFER,
        },
        priority=requirement.priority,
        work_key=requirement.work_key,
        work_requirement_id=requirement.id,
    )
    ctx.mark_work(requirement_id, WorkStatus.SPAWNED)
    spawned = ctx.new_event(
        "work_spawned",
        {"work_requirement_id": str(requirement.id), "work_key": requirement.work_key},
    )
    return ctx.complete(
        output={"spawned": True, "work_key": requirement.work_key},
        spawned_processes=[spec],
        emitted_events=[spawned],
    )


async def resistance_check(ctx: ProcessContext) -> ProcessResult:
    """The work process: analyse resistance, suspending for a missing measurement.

    Standard inputs come from ``ctx.view`` (the compiled Context): the current
    target of the analysed entity and the WorkRequirement being fulfilled.  The
    wafer measurement is an explicit lookup via ``ctx.state`` (kept as a special
    query, not a standard input).  Because the view is recompiled every
    activation, a resume sees the *current* target, not the suspend-time value.
    """
    entity = ctx.instance.input.get("entity")
    wafer = ctx.instance.input.get("wafer", DEFAULT_WAFER)
    observed_target = ctx.view.get_state(entity, "target") if ctx.view and entity else None

    if ctx.resume_point is None:
        measurement = ctx.state.get(f"measurement_{wafer}", "resistance")
        if measurement is None:
            ctx.logger.info("no measurement for %s; suspending", wafer)
            return ctx.suspend(
                resume_point="compare_resistance",
                waiting_for={"event_type": "measurement_completed", "wafer": wafer},
                saved_process_state={"wafer": wafer, "entity": entity},
            )
        return _finish(ctx, entity, wafer, measurement, observed_target)

    if ctx.resume_point == "compare_resistance":
        resistance = ctx.event.payload["resistance"]
        ctx.state.set(
            f"measurement_{wafer}", "resistance", resistance, source_event=ctx.event.id
        )
        return _finish(ctx, entity, wafer, resistance, observed_target)

    return ctx.fail(f"unknown resume_point: {ctx.resume_point!r}")


def _finish(ctx: ProcessContext, entity, wafer, resistance, observed_target) -> ProcessResult:
    """Emit completion + work_satisfied events and mark the requirement SATISFIED."""
    work = ctx.view.current_work_requirement if ctx.view else None
    completed = ctx.new_event(
        "resistance_analysis_completed",
        {
            "entity": entity,
            "wafer": wafer,
            "resistance": resistance,
            "work_key": ctx.instance.work_key,
        },
    )
    satisfied = ctx.new_event(
        "work_satisfied",
        {
            "work_requirement_id": str(ctx.instance.work_requirement_id)
            if ctx.instance.work_requirement_id
            else None,
            "work_key": ctx.instance.work_key,
        },
    )
    ctx.satisfy_work()
    return ctx.complete(
        output={
            "wafer": wafer,
            "resistance": resistance,
            "observed_target": observed_target,
            "work_requirement_id": str(work.id) if work is not None else None,
        },
        emitted_events=[completed, satisfied],
    )


async def reconcile_blocked_work(ctx: ProcessContext) -> ProcessResult:
    """Re-offer work that was blocked, now that a new competence exists.

    This is **not** event replay (Invariant 52 / spec §98).  Nothing is
    re-interpreted and no new WorkRequirement is created: the existing
    requirements are still there, still holding their ids and their provenance.
    All that changed is that the system can now do them, so they are handed
    back to the matcher.
    """
    assert ctx.event is not None and ctx.services is not None
    capability_name = ctx.event.payload.get("capability_name")
    provider_event = ctx.event.type == "provider_available"

    blocked_status = (
        WorkStatus.BLOCKED_PROVIDER
        if provider_event
        else WorkStatus.BLOCKED_CAPABILITY
    )
    blocked = ctx.services.get_work_by_status(blocked_status)
    reopened = []
    emitted = []
    for requirement in blocked:
        # Narrow to work this capability could plausibly unblock; a requirement
        # blocked on something else is left alone (spec §87).
        wanted = {r.name for r in requirement.required_capabilities}
        if not provider_event and capability_name is not None and capability_name not in wanted:
            continue
        if provider_event:
            selected_name = ctx.event.payload.get("definition_name")
            selected_version = ctx.event.payload.get("definition_version")
            if selected_name and requirement.selected_definition_name not in {
                None,
                selected_name,
            }:
                continue
            definition = ctx.services.get_definition(selected_name, selected_version)
            providers = ctx.services.get_provider_registry()
            if definition is None or providers is None:
                continue
            if not providers.has_eligible_provider(definition):
                continue
        ctx.mark_work(requirement.id, WorkStatus.EXPECTED)
        emitted.append(
            ctx.new_event("work_required", {"work_requirement_id": str(requirement.id)})
        )
        reopened.append(requirement.work_key)

    if reopened:
        ctx.logger.info(
            "capability %s unblocked %d work requirement(s): %s",
            capability_name,
            len(reopened),
            reopened,
        )
    return ctx.complete(
        output={
            "reopened": reopened,
            "capability": capability_name,
            "provider_available": provider_event,
        },
        emitted_events=emitted,
    )


RECONCILE_BLOCKED_WORK = ProcessDefinition(
    name="reconcile_blocked_work",
    version="1",
    handler="reconcile_blocked_work",
    trigger_event_types=("capability_available", "provider_available"),
    metadata={"role": "capability_reconciler"},
)


def bootstrap_work_intelligence(runtime) -> None:
    """Register all work-intelligence processes on ``runtime`` (idempotent)."""
    runtime.register_process(IMPACT_ANALYSIS, impact_analysis)
    runtime.register_process(WORK_MATCHER, work_matcher)
    runtime.register_process(MISSING_WORK_DETECTOR, missing_work_detector)
    runtime.register_process(WORK_SPAWNER, work_spawner)
    runtime.register_process(RECONCILE_BLOCKED_WORK, reconcile_blocked_work)
    runtime.register_process(RESISTANCE_CHECK, resistance_check)
