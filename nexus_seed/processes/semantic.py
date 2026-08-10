"""Semantic world-model processes: interpret_event and apply_state_delta.

These are ordinary Processes (no new primitive).  Together they realise:

    process_parameter_changed  (raw event)
        -> interpret_event      -> Observation + StateDelta  (persisted)
        -> state_delta_created  (event)
        -> apply_state_delta    -> world_state history + current
        -> state_changed        (event)

Phase 2B uses deterministic parsing only — no LLM.  Observation (what was read)
and StateDelta (what changed) are kept as distinct types even though the demo
mapping is 1:1, because that separation matters once interpretation is fuzzy.
"""

from __future__ import annotations

import uuid

from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..world.state_delta import StateConflict

INTERPRET = ProcessDefinition(
    name="interpret_event",
    version="1",
    handler="interpret_event",
    trigger_event_types=("process_parameter_changed",),
    metadata={"role": "interpreter"},
)

APPLY = ProcessDefinition(
    name="apply_state_delta",
    version="1",
    handler="apply_state_delta",
    trigger_event_types=("state_delta_created",),
    metadata={"role": "state_writer"},
)


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


async def interpret_event(ctx: ProcessContext) -> ProcessResult:
    """Read a ``process_parameter_changed`` event into an Observation + StateDelta."""
    assert ctx.event is not None
    payload = ctx.event.payload
    parameter = payload["parameter"]
    old = payload.get("old")
    new = payload["new"]
    unit = payload.get("unit")

    observation = ctx.observe(
        subject=parameter,
        predicate="target_changed",
        extracted={"old": old, "new": new, "unit": unit},
        confidence=1.0,
    )
    delta = ctx.propose_delta(
        entity=parameter,
        attribute="target",
        old_value=old,
        new_value=new,
        observation=observation,
        confidence=1.0,
        reason=f"{parameter} target changed",
    )

    created = ctx.new_event(
        "state_delta_created",
        {
            "entity": delta.entity,
            "attribute": delta.attribute,
            "old_value": delta.old_value,
            "new_value": delta.new_value,
            "source_event_id": str(delta.source_event_id) if delta.source_event_id else None,
            "observation_id": str(delta.observation_id) if delta.observation_id else None,
            "state_delta_id": str(delta.id),
            "confidence": delta.confidence,
            "unit": unit,
        },
    )
    return ctx.complete(
        output={"observation_id": str(observation.id), "state_delta_id": str(delta.id)},
        emitted_events=[created],
    )


async def apply_state_delta(ctx: ProcessContext) -> ProcessResult:
    """Validate a StateDelta against current state and apply it to world state."""
    assert ctx.event is not None
    p = ctx.event.payload
    entity = p["entity"]
    attribute = p["attribute"]
    old_value = p["old_value"]
    new_value = p["new_value"]

    entry = ctx.state.get_entry(entity, attribute)
    if entry is not None and entry.value != old_value:
        # Domain-level conflict: do not apply, leave current state untouched.
        return ctx.fail(
            StateConflict(entity, attribute, expected=old_value, actual=entry.value)
        )

    next_version = (entry.version if entry is not None else 0) + 1
    ctx.state.set(
        entity,
        attribute,
        new_value,
        source_event=_uuid(p.get("source_event_id")),
        observation_id=_uuid(p.get("observation_id")),
        state_delta_id=_uuid(p.get("state_delta_id")),
        confidence=p.get("confidence", 1.0),
    )

    changed = ctx.new_event(
        "state_changed",
        {
            "entity": entity,
            "attribute": attribute,
            "old_value": old_value,
            "new_value": new_value,
            "version": next_version,
            "state_delta_id": p.get("state_delta_id"),
        },
    )
    return ctx.complete(
        output={"entity": entity, "attribute": attribute, "version": next_version},
        emitted_events=[changed],
    )


def bootstrap_semantic(runtime) -> None:
    """Register the interpret/apply processes on ``runtime`` (idempotent)."""
    runtime.register_process(INTERPRET, interpret_event)
    runtime.register_process(APPLY, apply_state_delta)
