"""AT42–AT47 (spec §44–§47): a budget must not cost an event.

A bound that loses work is not a bound, it is a leak.  Everything the delivery
ledger promised in Phase 3F still holds when a slice stops early: an event that
was appended is still owed, an event that was routed is still DELIVERED, and no
delivery is left in the in-flight state just because the caller went away.
"""

from __future__ import annotations

from drain_helpers import (
    RECORDED,
    DrainBudget,
    drain_runtime,
    pending,
    register_chain,
    register_fanout,
    step,
)

from nexus_seed.delivery.models import EventDeliveryStatus


def statuses(runtime) -> list[str]:
    return [d.status.value for d in runtime.get_event_deliveries()]


async def test_an_event_left_unrouted_is_still_owed(tmp_path):
    """AT42."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=4)

    await runtime.submit_event(step(0), DrainBudget(max_activations=1))

    assert runtime.last_drain.exhausted
    assert pending(runtime) == 1
    assert EventDeliveryStatus.PENDING.value in statuses(runtime)
    runtime.close()


async def test_nothing_is_left_in_the_in_flight_state(tmp_path):
    """AT43: DELIVERING is a marker for a crash, not for a yield."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=4)

    await runtime.submit_event(step(0), DrainBudget(max_activations=2))

    assert EventDeliveryStatus.DELIVERING.value not in statuses(runtime)
    runtime.close()


async def test_a_dispatch_budget_bounds_the_routing_half(tmp_path):
    """AT44: the two halves are budgeted separately (spec §47)."""
    runtime, _ = drain_runtime(tmp_path)
    register_fanout(runtime, width=4)

    await runtime.submit_event(step(0), DrainBudget(max_dispatches=2))

    assert runtime.last_drain.dispatches == 2
    assert runtime.last_drain.exhausted
    assert pending(runtime) > 0
    runtime.close()


async def test_neither_half_can_eat_the_whole_budget(tmp_path):
    """AT45: alternation, so a big queue does not starve execution.

    With four events waiting and a dispatch cap of one per pass, the slice
    still runs processes — a drain that dispatched everything first would
    report activations of zero.
    """
    runtime, _ = drain_runtime(tmp_path)
    register_fanout(runtime, width=4)

    await runtime.submit_event(step(0), DrainBudget(max_dispatches=3))

    assert runtime.last_drain.dispatches == 3
    assert runtime.last_drain.activations >= 1
    assert len(RECORDED) >= 1
    runtime.close()


async def test_every_event_is_eventually_delivered_across_slices(tmp_path):
    """AT46: the fan-out completes, one small slice at a time."""
    runtime, _ = drain_runtime(tmp_path)
    register_fanout(runtime, width=5)

    await runtime.submit_event(step(0), DrainBudget(max_activations=1))
    for _ in range(30):
        if not runtime.last_drain.has_remaining:
            break
        await runtime.drain(DrainBudget(max_activations=1))

    assert pending(runtime) == 0
    assert set(statuses(runtime)) == {EventDeliveryStatus.DELIVERED.value}
    # One activation for the trigger plus one per fanned-out event.
    assert len(RECORDED) == 6
    runtime.close()


async def test_an_event_is_not_delivered_twice_across_a_yield(tmp_path):
    """AT47: resuming is not replaying."""
    runtime, _ = drain_runtime(tmp_path)
    register_fanout(runtime, width=3)

    await runtime.submit_event(step(0), DrainBudget(max_activations=2))
    await runtime.drain()

    seen = [(r["definition"], r["n"]) for r in RECORDED]
    assert len(seen) == len(set(seen)), seen
    assert pending(runtime) == 0
    runtime.close()
