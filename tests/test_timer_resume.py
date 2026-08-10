"""Timer event: a process suspended on a timer resumes when the timer fires."""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
)
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


async def timer_handler(ctx: ProcessContext) -> ProcessResult:
    """Suspend on a 5s timer, then complete when it fires."""
    if ctx.resume_point is None:
        return ctx.suspend_on_timer(
            resume_point="after_timer", delay=5, saved_process_state={"n": 1}
        )
    ctx.state.set("timer", "fired", True, source_event=ctx.event.id)
    return ctx.complete(output={"resumed_from": ctx.saved_process_state})


DEFINITION = ProcessDefinition("timer_demo", "1", "timer_demo", ("start",))


async def test_timer_suspend_and_resume(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "timer.db", clock=clock)
    runtime.register_process(DEFINITION, timer_handler)

    await runtime.submit_event(Event("start", "test", {}))

    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.SUSPENDED
    assert len(runtime.timer_store.all()) == 1

    # Timer not due yet: tick is a no-op for this process.
    await runtime.tick()
    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.SUSPENDED

    # Advance past the timer: it fires as a normal event and resumes the process.
    clock.advance(5)
    produced = await runtime.tick()

    final = runtime.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.COMPLETED
    assert runtime.state_store.get("timer", "fired") is True
    assert any(e.type == "timer_fired" for e in produced)
    # Timer is not delivered a second time.
    await runtime.tick()
    assert len([t for t in runtime.timer_store.all() if not t.fired]) == 0
    runtime.close()


async def test_timer_survives_runtime_restart(tmp_path):
    db_path = tmp_path / "timer_restart.db"
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    runtime = Runtime(db_path, clock=clock)
    runtime.register_process(DEFINITION, timer_handler)
    await runtime.submit_event(Event("start", "test", {}))
    instance_id = runtime.process_store.all_instances()[0].id
    runtime.close()

    # Rebuild from SQLite; the pending timer is still there.
    clock2 = ManualClock(datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc))
    runtime2 = Runtime(db_path, clock=clock2)
    runtime2.register_process(DEFINITION, timer_handler)
    await runtime2.tick()

    assert runtime2.process_store.get_instance(instance_id).status is ProcessStatus.COMPLETED
    runtime2.close()
