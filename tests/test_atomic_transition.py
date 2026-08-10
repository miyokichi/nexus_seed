"""Atomic process transition: all effects commit together, or none do."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
)
from nexus_seed.runtime.runtime import Runtime


async def complete_with_effects(ctx: ProcessContext) -> ProcessResult:
    """Stage a state change and emit an event, then complete."""
    ctx.state.set("atom", "value", 7, source_event=ctx.event.id)
    side_effect = ctx.new_event("side_effect", {"k": 7})
    return ctx.complete(output={"ok": True}, emitted_events=[side_effect])


DEFINITION = ProcessDefinition("atom", "1", "atom", ("go",))


async def test_all_effects_commit_together(tmp_path):
    runtime = Runtime(tmp_path / "atom.db")
    runtime.register_process(DEFINITION, complete_with_effects)

    produced = await runtime.submit_event(Event("go", "test", {}))

    assert runtime.state_store.get("atom", "value") == 7
    assert [e.type for e in produced] == ["side_effect"]
    assert len(runtime.event_store.by_type("side_effect")) == 1
    assert runtime.process_store.all_instances()[0].status is ProcessStatus.COMPLETED
    runtime.close()


async def test_partial_update_rolls_back_on_failure(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "atom.db")
    runtime.register_process(DEFINITION, complete_with_effects)

    # Inject an exception in the middle of the commit: fail the final instance
    # write (which happens after the state write and event append).
    original_save = runtime.process_store.save_instance

    def failing_save(instance):
        if instance.status is ProcessStatus.COMPLETED:
            raise RuntimeError("injected mid-transaction failure")
        return original_save(instance)

    monkeypatch.setattr(runtime.process_store, "save_instance", failing_save)

    await runtime.submit_event(Event("go", "test", {}))

    # Nothing from the activation persisted: not the state, not the event.
    assert runtime.state_store.get("atom", "value") is None
    assert runtime.event_store.by_type("side_effect") == []
    # The instance ended FAILED, not half-COMPLETED.
    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.FAILED
    runtime.close()
