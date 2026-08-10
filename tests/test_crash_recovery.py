"""Crash recovery: an orphaned RUNNING process is recovered on restart."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessInstance,
    ProcessResult,
    ProcessStatus,
)
from nexus_seed.runtime.runtime import Runtime


async def job_handler(ctx: ProcessContext) -> ProcessResult:
    """Mark an entity done and complete."""
    ctx.state.set(ctx.event.payload["entity"], "done", True, source_event=ctx.event.id)
    return ctx.complete(output={"ok": True})


DEFINITION = ProcessDefinition("job", "1", "job", ("go",))


async def test_orphaned_running_instance_is_recovered(tmp_path):
    db_path = tmp_path / "crash.db"

    # --- Simulate a crash: leave an instance stuck in RUNNING. ---
    runtime = Runtime(db_path)
    runtime.register_process(DEFINITION, job_handler)
    event = Event("go", "test", {"entity": "x"})
    runtime.event_store.append(event)
    stuck = ProcessInstance(
        definition_name="job",
        definition_version="1",
        status=ProcessStatus.RUNNING,
        pending_event_id=event.id,
    )
    runtime.process_store.save_instance(stuck)
    runtime.close()

    # --- Restart: recovery sweep runs in the constructor. ---
    runtime2 = Runtime(db_path)
    runtime2.register_process(DEFINITION, job_handler)

    recovered = runtime2.process_store.get_instance(stuck.id)
    assert recovered.status is ProcessStatus.RUNNABLE  # not left RUNNING forever

    produced = await runtime2.run_pending()

    final = runtime2.process_store.get_instance(stuck.id)
    assert final.status is ProcessStatus.COMPLETED
    assert runtime2.state_store.get("x", "done") is True
    assert produced == []
    runtime2.close()
