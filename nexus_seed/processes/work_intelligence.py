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
from ..work.rules import (
    WORK_PRIORITY,
    build_work_key,
    expected_work_types,
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
        )
        created.append(work_key)
        emitted.append(
            ctx.new_event("work_required", {"work_requirement_id": str(requirement.id)})
        )

    return ctx.complete(output={"expected_work": created}, emitted_events=emitted)


async def work_matcher(ctx: ProcessContext) -> ProcessResult:
    """Decide whether a requirement is already covered by existing work."""
    assert ctx.event is not None and ctx.services is not None
    requirement_id = _uuid(ctx.event.payload["work_requirement_id"])
    requirement = ctx.services.get_work_requirement(requirement_id)
    if requirement is None:
        return ctx.fail(f"work requirement {requirement_id} not found")

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


async def missing_work_detector(ctx: ProcessContext) -> ProcessResult:
    """Decide whether a spawn is required for a matched requirement."""
    assert ctx.event is not None
    p = ctx.event.payload
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

    target = process_for_work_type(requirement.work_type)
    if target is None:
        ctx.mark_work(requirement_id, WorkStatus.CANCELLED)
        return ctx.complete(
            output={"spawned": False, "reason": "no process for work_type"}
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


def bootstrap_work_intelligence(runtime) -> None:
    """Register all work-intelligence processes on ``runtime`` (idempotent)."""
    runtime.register_process(IMPACT_ANALYSIS, impact_analysis)
    runtime.register_process(WORK_MATCHER, work_matcher)
    runtime.register_process(MISSING_WORK_DETECTOR, missing_work_detector)
    runtime.register_process(WORK_SPAWNER, work_spawner)
    runtime.register_process(RESISTANCE_CHECK, resistance_check)
