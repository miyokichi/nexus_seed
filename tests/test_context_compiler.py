"""Minimal context: no requirements -> only process_instance + trigger_event."""

from __future__ import annotations

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


async def test_minimal_context_has_no_full_world_state(tmp_path):
    runtime = Runtime(tmp_path / "min.db")
    definition = ProcessDefinition("min", "1", "min")  # no context_requirements
    runtime.register_process(definition, _noop)

    # World state exists, but a minimal context must not pull it in.
    runtime.state_store.set("D1_CD", "target", 45)
    runtime.state_store.set("UNRELATED_X", "value", 999)

    instance = ProcessInstance(definition_name="min", definition_version="1")
    runtime.process_store.save_instance(instance)
    trigger = Event("go", "test", {"k": 1})
    runtime.event_store.append(trigger)

    view = runtime.context_compiler.compile(
        definition=definition, process_instance=instance, trigger_event=trigger
    )

    assert view.process_instance.id == instance.id
    assert view.trigger_event is not None
    assert view.trigger_event.id == trigger.id
    assert view.world_state == {}
    assert view.recent_events == []
    assert view.metadata.item_counts["world_state"] == 0
    runtime.close()
