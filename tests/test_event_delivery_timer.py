"""AT15 (spec §66, §38): a fired timer is an event like any other.

Timers used to route their own event inline.  Now they append it — which
records the obligation — and the ordinary drain routes it.  A crash in between
delays the wake-up; it does not cancel it.  And the timer is marked fired
first, so the effect cannot double-fire.
"""

from __future__ import annotations

from delivery_helpers import EPOCH, RECORDED, instances_named, manual_runtime, status_of

from nexus_seed.context.requirements import ContextRequirements, ContinuationReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime

SLEEPER = ProcessDefinition(
    name="sleeper",
    version="1",
    handler="sleeper",
    trigger_event_types=("start_sleep",),
    context_requirements=ContextRequirements(
        include_trigger_event=True, continuation=ContinuationReq(include=True)
    ),
)


async def sleeper(ctx):
    if ctx.resume_point == "woken":
        RECORDED.append({"woken": True, "event_id": str(ctx.event.id)})
        return ctx.complete(output={"woken": True})
    return ctx.suspend_on_timer(resume_point="woken", delay=30)


def build(runtime):
    RECORDED.clear()
    runtime.register_process(SLEEPER, sleeper)
    return runtime


async def test_a_fired_timer_event_gets_an_obligation(tmp_path):
    runtime, clock = manual_runtime(tmp_path)
    build(runtime)

    await runtime.submit_event(Event("start_sleep", "test", {}))
    clock.advance(30)
    await runtime.tick()

    fired = runtime.event_store.by_type("timer_fired")[0]
    assert status_of(runtime, fired.id) == "DELIVERED"
    assert len(RECORDED) == 1
    runtime.close()


async def test_a_crash_between_firing_and_routing_still_wakes_the_process(tmp_path):
    """AT15: the timer event is durable before it is acted on."""
    db_path = tmp_path / "t.db"
    clock = ManualClock(EPOCH)

    runtime = build(Runtime(db_path, clock=clock))
    await runtime.submit_event(Event("start_sleep", "test", {}))
    instance = instances_named(runtime, "sleeper")[0]
    assert instance.status is ProcessStatus.SUSPENDED

    # Fire the timer without draining — the crash window.
    clock.advance(30)
    for timer in runtime.timer_store.due(clock.now()):
        runtime.timer_store.mark_fired(timer.id)
        runtime.event_store.append(
            Event("timer_fired", "runtime.timer", timer.payload)
        )
    fired = runtime.event_store.by_type("timer_fired")[0]
    assert status_of(runtime, fired.id) == "PENDING"
    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.SUSPENDED
    runtime.close()

    # --- restart ---
    runtime2 = build(Runtime(db_path, clock=clock))
    assert runtime2.get_pending_event_delivery_count() == 1

    await runtime2.run_pending()

    assert status_of(runtime2, fired.id) == "DELIVERED"
    assert runtime2.process_store.get_instance(instance.id).status is ProcessStatus.COMPLETED
    assert len(RECORDED) == 1
    runtime2.close()


async def test_the_timer_does_not_fire_twice_across_the_restart(tmp_path):
    """The timer is marked fired before the event is appended."""
    db_path = tmp_path / "t.db"
    clock = ManualClock(EPOCH)

    runtime = build(Runtime(db_path, clock=clock))
    await runtime.submit_event(Event("start_sleep", "test", {}))
    clock.advance(30)
    for timer in runtime.timer_store.due(clock.now()):
        runtime.timer_store.mark_fired(timer.id)
        runtime.event_store.append(Event("timer_fired", "runtime.timer", timer.payload))
    runtime.close()

    runtime2 = build(Runtime(db_path, clock=clock))
    clock.advance(600)
    await runtime2.tick()
    await runtime2.tick()

    assert len(runtime2.event_store.by_type("timer_fired")) == 1
    assert len(RECORDED) == 1
    runtime2.close()


async def test_a_long_running_observer_keeps_its_deliveries_settled(tmp_path):
    """Many timer cycles must not accumulate outstanding obligations."""
    from resource_helpers import resource_runtime, watched_tree

    root = watched_tree(tmp_path)
    clock = ManualClock(EPOCH)
    runtime = Runtime(tmp_path / "obs.db", clock=clock)
    resource_runtime(runtime, root, observer=True)

    await runtime.submit_event(
        Event("start_watch_files", "operator", {"adapter_id": "local_file", "poll_interval": 30})
    )
    for _ in range(5):
        clock.advance(30)
        await runtime.tick()

    assert len(runtime.event_store.by_type("timer_fired")) == 5
    assert runtime.get_pending_event_delivery_count() == 0
    assert all(d.status.value == "DELIVERED" for d in runtime.get_event_deliveries())
    runtime.close()
