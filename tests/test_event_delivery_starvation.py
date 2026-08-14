"""AT8 (spec §59, §31): one stuck event must not hold up the rest.

A queue that stops at its first bad item is worse than no queue: a single
malformed event would silently freeze the whole system.  Deliveries in backoff
are skipped, not waited on.
"""

from __future__ import annotations

from delivery_helpers import (
    RECORDED,
    break_router,
    heal_router,
    manual_runtime,
    ping,
    register_noter,
    status_of,
)

from nexus_seed.core.event import Event


async def test_a_failing_event_does_not_block_the_next_one(tmp_path):
    """AT8."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)

    bad = Event("poison", "test", {})
    good = ping({"n": 1})
    runtime.event_store.append(bad)
    runtime.event_store.append(good)

    # The router only fails on the poison event.
    break_router(runtime, failures=99, only_type="poison")
    await runtime.run_pending()

    assert status_of(runtime, bad.id) == "RETRY_WAIT"
    assert status_of(runtime, good.id) == "DELIVERED"
    assert len(RECORDED) == 1
    runtime.close()


async def test_later_events_keep_flowing_while_one_retries(tmp_path):
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=99, only_type="poison")

    poison = Event("poison", "test", {})
    runtime.event_store.append(poison)
    await runtime.run_pending()

    for n in range(3):
        clock.advance(600)
        await runtime.submit_event(ping({"n": n}))

    assert len(RECORDED) == 3
    assert status_of(runtime, poison.id) == "RETRY_WAIT"
    # The poison event is still owed, and still counted.
    assert runtime.get_pending_event_delivery_count() == 1
    runtime.close()


async def test_the_stuck_event_still_completes_once_the_cause_is_fixed(tmp_path):
    """Skipped is not abandoned: it keeps its place in the ledger."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime, trigger="poison")
    break_router(runtime, failures=2, only_type="poison")

    poison = Event("poison", "test", {})
    runtime.event_store.append(poison)
    await runtime.run_pending()
    assert status_of(runtime, poison.id) == "RETRY_WAIT"

    clock.advance(600)
    await runtime.tick()  # second failure
    assert status_of(runtime, poison.id) == "RETRY_WAIT"

    clock.advance(600)
    await runtime.tick()  # succeeds

    assert status_of(runtime, poison.id) == "DELIVERED"
    assert len(RECORDED) == 1
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_a_batch_full_of_waiting_events_still_makes_progress(tmp_path):
    """The batch limit counts what is *dispatchable*, not what is outstanding."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=99, only_type="poison")

    for _ in range(3):
        runtime.event_store.append(Event("poison", "test", {}))
    await runtime.run_pending()
    assert runtime.get_pending_event_delivery_count() == 3

    good = ping()
    await runtime.submit_event(good)

    assert status_of(runtime, good.id) == "DELIVERED"
    assert len(RECORDED) == 1
    runtime.close()
