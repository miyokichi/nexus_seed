"""Relevant events: `recent: N` selects the N most recent, deterministically."""

from __future__ import annotations

from nexus_seed.context.requirements import ContextRequirements, EventsReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessInstance,
    ProcessResult,
)
from nexus_seed.runtime.runtime import Runtime


async def _noop(ctx: ProcessContext) -> ProcessResult:
    return ctx.complete(output={})


async def test_recent_events_limit(tmp_path):
    runtime = Runtime(tmp_path / "ev.db")
    definition = ProcessDefinition(
        "ev", "1", "ev", context_requirements=ContextRequirements(events=EventsReq(recent=2))
    )
    runtime.register_process(definition, _noop)

    for i in range(4):
        runtime.event_store.append(Event(f"e{i}", "test", {"i": i}))

    instance = ProcessInstance(definition_name="ev", definition_version="1")
    runtime.process_store.save_instance(instance)

    view = runtime.context_compiler.compile(
        definition=definition, process_instance=instance
    )

    assert len(view.recent_events) == 2
    assert {e.type for e in view.recent_events} == {"e2", "e3"}
    # Deterministic ascending order by occurred_at.
    times = [e.occurred_at for e in view.recent_events]
    assert times == sorted(times)
    runtime.close()


async def test_related_events_by_entity(tmp_path):
    runtime = Runtime(tmp_path / "ev2.db")
    definition = ProcessDefinition(
        "ev2",
        "1",
        "ev2",
        context_requirements=ContextRequirements(
            events=EventsReq(related_entities=["D1_CD"])
        ),
    )
    runtime.register_process(definition, _noop)

    runtime.event_store.append(Event("process_parameter_changed", "world", {"parameter": "D1_CD"}))
    runtime.event_store.append(Event("process_parameter_changed", "world", {"parameter": "OTHER"}))

    instance = ProcessInstance(definition_name="ev2", definition_version="1")
    runtime.process_store.save_instance(instance)

    view = runtime.context_compiler.compile(
        definition=definition, process_instance=instance
    )
    assert len(view.recent_events) == 1
    assert view.recent_events[0].payload["parameter"] == "D1_CD"
    runtime.close()
