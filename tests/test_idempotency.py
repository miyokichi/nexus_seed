"""Idempotency: delivering the same event twice applies its effects once."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessContext, ProcessDefinition, ProcessResult
from nexus_seed.runtime.runtime import Runtime


async def counter_handler(ctx: ProcessContext) -> ProcessResult:
    """Increment a per-entity counter and emit a done event."""
    entity = ctx.event.payload["entity"]
    current = ctx.state.get(entity, "count", 0)
    ctx.state.set(entity, "count", current + 1, source_event=ctx.event.id)
    done = ctx.new_event("count_done", {"entity": entity})
    return ctx.complete(output={"count": current + 1}, emitted_events=[done])


DEFINITION = ProcessDefinition("counter", "1", "counter", ("count_requested",))


async def test_duplicate_event_is_ignored(tmp_path):
    runtime = Runtime(tmp_path / "idem.db")
    runtime.register_process(DEFINITION, counter_handler)

    event = Event("count_requested", "test", {"entity": "widget"})

    first = await runtime.submit_event(event)
    second = await runtime.submit_event(event)  # same id, re-delivered

    # Second delivery is a no-op.
    assert [e.type for e in first] == ["count_done"]
    assert second == []

    # No duplicate process, no double state change, no double completion event.
    assert len(runtime.process_store.all_instances()) == 1
    assert runtime.state_store.get("widget", "count") == 1
    assert len(runtime.event_store.by_type("count_done")) == 1
    runtime.close()
