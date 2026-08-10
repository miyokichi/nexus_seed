"""Tests for basic process execution (trigger -> run -> complete)."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessContext, ProcessDefinition, ProcessResult, ProcessStatus
from nexus_seed.runtime.runtime import Runtime


COUNTER = ProcessDefinition(
    name="counter",
    version="1",
    handler="counter",
    trigger_event_types=("count_requested",),
)


async def counter_handler(ctx: ProcessContext) -> ProcessResult:
    """Increment a per-entity counter in world state and emit a done event."""
    entity = ctx.event.payload["entity"]
    current = ctx.state.get(entity, "count", 0)
    new_value = current + ctx.event.payload.get("by", 1)
    ctx.state.set(entity, "count", new_value, source_event=ctx.event.id)
    done = ctx.new_event("count_done", {"entity": entity, "count": new_value})
    return ctx.complete(output={"count": new_value}, emitted_events=[done])


def _runtime(tmp_path) -> Runtime:
    runtime = Runtime(tmp_path / "run.db")
    runtime.register_process(COUNTER, counter_handler)
    return runtime


async def test_trigger_runs_process_to_completion(tmp_path):
    runtime = _runtime(tmp_path)
    produced = await runtime.submit_event(
        Event("count_requested", "test", {"entity": "widget", "by": 3})
    )

    assert runtime.state_store.get("widget", "count") == 3
    instances = runtime.process_store.all_instances()
    assert len(instances) == 1
    assert instances[0].status is ProcessStatus.COMPLETED
    assert instances[0].local_state["output"] == {"count": 3}

    assert [e.type for e in produced] == ["count_done"]
    runtime.close()


async def test_emitted_event_preserves_caus_and_correlation(tmp_path):
    runtime = _runtime(tmp_path)
    trigger = Event("count_requested", "test", {"entity": "gadget"})
    produced = await runtime.submit_event(trigger)

    done = produced[0]
    assert done.causation_id == trigger.id
    assert done.correlation_id == trigger.id  # trigger had no correlation of its own
    runtime.close()


async def test_multiple_instances_from_one_definition(tmp_path):
    runtime = _runtime(tmp_path)
    await runtime.submit_event(Event("count_requested", "test", {"entity": "a"}))
    await runtime.submit_event(Event("count_requested", "test", {"entity": "b"}))

    instances = runtime.process_store.all_instances()
    assert len(instances) == 2
    assert all(i.status is ProcessStatus.COMPLETED for i in instances)
    assert runtime.state_store.get("a", "count") == 1
    assert runtime.state_store.get("b", "count") == 1
    runtime.close()


async def test_handler_exception_marks_instance_failed(tmp_path):
    runtime = Runtime(tmp_path / "fail.db")

    async def boom(ctx: ProcessContext) -> ProcessResult:
        raise RuntimeError("boom")

    definition = ProcessDefinition("boom", "1", "boom", ("go",))
    runtime.register_process(definition, boom)

    await runtime.submit_event(Event("go", "test", {}))
    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.FAILED
    assert "boom" in instance.local_state["error"]
    runtime.close()
