"""AT7 (spec §58, §18–§19): a failed routing attempt is rescheduled, not lost.

Routing can fail for reasons that have nothing to do with the event — a locked
database, a bug in one definition's trigger matching.  Phase 3F treats that as
transient bookkeeping: the obligation stays outstanding, backs off, and is
retried.  Giving up is the exception, not the default.
"""

from __future__ import annotations

from delivery_helpers import (
    RECORDED,
    break_router,
    heal_router,
    instances_named,
    manual_runtime,
    ping,
    register_noter,
    status_of,
)

from nexus_seed.delivery.models import EventDeliveryStatus


async def test_a_failed_attempt_backs_off_then_succeeds(tmp_path):
    """AT7."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=1)

    event = ping()
    await runtime.submit_event(event)

    delivery = runtime.get_event_delivery(event.id)
    assert delivery.status is EventDeliveryStatus.RETRY_WAIT
    assert delivery.attempt_count == 1
    assert "router exploded" in delivery.last_error
    assert delivery.next_attempt_at is not None
    assert RECORDED == []

    # Before the backoff elapses, the sweep does not touch it.
    assert runtime.dispatch_pending_events() == 0

    clock.advance(5)
    heal_router(runtime)
    await runtime.tick()

    settled = runtime.get_event_delivery(event.id)
    assert settled.status is EventDeliveryStatus.DELIVERED
    assert settled.attempt_count == 2
    assert settled.last_error is None
    assert len(RECORDED) == 1
    runtime.close()


async def test_backoff_grows_across_repeated_failures(tmp_path):
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=3)

    event = ping()
    await runtime.submit_event(event)
    delays = []
    for _ in range(2):
        delivery = runtime.get_event_delivery(event.id)
        delays.append((delivery.next_attempt_at - clock.now()).total_seconds())
        clock.advance(600)
        runtime.dispatch_pending_events()

    assert delays[1] > delays[0]
    assert runtime.get_event_delivery(event.id).attempt_count == 3
    runtime.close()


async def test_the_event_is_never_abandoned_by_default(tmp_path):
    """No max_attempts: an event keeps failing loudly rather than vanishing."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=10)

    event = ping()
    await runtime.submit_event(event)
    for _ in range(5):
        clock.advance(600)
        runtime.dispatch_pending_events()

    delivery = runtime.get_event_delivery(event.id)
    assert delivery.status is EventDeliveryStatus.RETRY_WAIT
    assert delivery.outstanding
    assert runtime.get_failed_event_deliveries() == []
    runtime.close()


async def test_a_configured_attempt_limit_settles_the_delivery(tmp_path):
    """Spec §78: a persistent failure can be parked for a human, with a reason."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    runtime.dispatcher.max_attempts = 3
    break_router(runtime, failures=10)

    event = ping()
    await runtime.submit_event(event)
    for _ in range(5):
        clock.advance(600)
        runtime.dispatch_pending_events()

    delivery = runtime.get_event_delivery(event.id)
    assert delivery.status is EventDeliveryStatus.FAILED
    assert "router exploded" in delivery.last_error
    assert delivery.attempt_count == 3
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_a_failed_route_creates_no_partial_activation(tmp_path):
    """AT4: routing and acknowledgement roll back together."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=1)

    event = ping()
    await runtime.submit_event(event)

    assert instances_named(runtime, "noter") == []
    assert status_of(runtime, event.id) == "RETRY_WAIT"

    clock.advance(5)
    heal_router(runtime)
    await runtime.tick()

    # Exactly one process, created by the retry — not two.
    assert len(instances_named(runtime, "noter")) == 1
    runtime.close()


async def test_a_retry_does_not_duplicate_downstream_work(tmp_path):
    """Delivery is at-least-once; the outcome is once (spec §24–§25)."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=2)

    event = ping()
    await runtime.submit_event(event)
    for _ in range(3):
        clock.advance(600)
        heal_router(runtime) if runtime.get_event_delivery(
            event.id
        ).attempt_count >= 2 else None
        runtime.dispatch_pending_events()

    await runtime.run_pending()
    assert len(instances_named(runtime, "noter")) == 1
    assert len(RECORDED) == 1
    runtime.close()
