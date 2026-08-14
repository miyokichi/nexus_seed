"""AT1–AT3 (spec §52–§54): the dispatcher, and what "delivered" means.

Delivery is not "we called route()".  It is "the routing result is committed",
which is why the acknowledgement rides in the same transaction as the
activations it produced (spec §14–§15).
"""

from __future__ import annotations

from delivery_helpers import (
    RECORDED,
    instances_named,
    manual_runtime,
    ping,
    register_noter,
    status_of,
)

from nexus_seed.core.event import Event
from nexus_seed.delivery.models import EventDeliveryStatus
from nexus_seed.runtime.runtime import Runtime


async def test_a_new_event_gets_a_delivery_and_ends_delivered(tmp_path):
    """AT1."""
    runtime = Runtime(tmp_path / "d.db")
    register_noter(runtime)

    event = ping()
    await runtime.submit_event(event)

    deliveries = runtime.get_event_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].event_id == event.id
    assert deliveries[0].status is EventDeliveryStatus.DELIVERED
    assert deliveries[0].attempt_count == 1
    assert deliveries[0].delivered_at is not None
    runtime.close()


async def test_an_event_with_no_subscriber_is_still_delivered(tmp_path):
    """AT2: delivery means the router got it, not that a process started."""
    runtime = Runtime(tmp_path / "d.db")

    event = await_event = Event("nobody_listens", "test", {})
    await runtime.submit_event(await_event)

    assert status_of(runtime, event.id) == "DELIVERED"
    assert runtime.process_store.all_instances() == []
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_delivery_activates_the_matching_process(tmp_path):
    """AT3."""
    runtime = Runtime(tmp_path / "d.db")
    register_noter(runtime)

    event = ping({"n": 1})
    await runtime.submit_event(event)

    assert len(RECORDED) == 1
    assert RECORDED[0]["event_id"] == str(event.id)
    assert len(instances_named(runtime, "noter")) == 1
    assert status_of(runtime, event.id) == "DELIVERED"
    runtime.close()


async def test_the_activation_records_the_event_that_triggered_it(tmp_path):
    """The routing idempotency key (spec §16)."""
    runtime = Runtime(tmp_path / "d.db")
    register_noter(runtime)
    event = ping()
    await runtime.submit_event(event)

    instance = instances_named(runtime, "noter")[0]
    assert instance.trigger_event_id == event.id
    # Unlike pending_event_id, it survives the activation committing.
    assert instance.pending_event_id is None
    runtime.close()


async def test_re_dispatching_a_delivered_event_starts_nothing(tmp_path):
    """No replay (spec §28), and the routing guard would catch it anyway."""
    runtime = Runtime(tmp_path / "d.db")
    register_noter(runtime)
    event = ping()
    await runtime.submit_event(event)

    # Force the obligation back to PENDING and sweep again.
    runtime.event_delivery_store.db.execute(
        "UPDATE event_deliveries SET status = 'PENDING' WHERE event_id = ?",
        (str(event.id),),
    )
    runtime.dispatch_pending_events()

    assert len(instances_named(runtime, "noter")) == 1
    assert status_of(runtime, event.id) == "DELIVERED"
    runtime.close()


async def test_events_are_dispatched_in_persistence_order(tmp_path):
    runtime = Runtime(tmp_path / "d.db")
    register_noter(runtime)

    events = [ping({"n": n}) for n in range(3)]
    for event in events:
        runtime.event_store.append(event)
    await runtime.run_pending()

    assert [r["event_id"] for r in RECORDED] == [str(e.id) for e in events]
    runtime.close()


async def test_dispatching_is_idempotent_when_nothing_is_outstanding(tmp_path):
    runtime = Runtime(tmp_path / "d.db")
    register_noter(runtime)
    await runtime.submit_event(ping())

    assert runtime.dispatch_pending_events() == 0
    assert len(instances_named(runtime, "noter")) == 1
    runtime.close()


async def test_a_delivery_for_a_missing_event_fails_rather_than_spins(tmp_path):
    """An obligation we can never fulfil must not retry forever."""
    import uuid

    runtime = Runtime(tmp_path / "d.db")
    ghost = uuid.uuid4()
    runtime.event_delivery_store.create_for_event(ghost)

    runtime.dispatch_pending_events()

    delivery = runtime.get_event_delivery(ghost)
    assert delivery.status is EventDeliveryStatus.FAILED
    assert delivery.last_error == "event not found"
    assert runtime.get_failed_event_deliveries() != []
    runtime.close()


async def test_the_batch_limit_bounds_one_sweep(tmp_path):
    runtime, _ = manual_runtime(tmp_path)
    register_noter(runtime)
    for n in range(5):
        runtime.event_store.append(ping({"n": n}))

    delivered = runtime.dispatch_pending_events(limit=2)

    assert delivered == 2
    assert runtime.get_pending_event_delivery_count() == 3
    runtime.close()


async def test_health_reports_the_queue(tmp_path):
    """Spec §75-§76: operational visibility without a UI."""
    runtime, _ = manual_runtime(tmp_path)
    register_noter(runtime)
    runtime.event_store.append(ping())

    before = runtime.get_delivery_health()
    assert before["pending"] == 1
    assert before["outstanding"] == 1
    assert before["oldest_pending_age_seconds"] is not None

    await runtime.run_pending()

    after = runtime.get_delivery_health()
    assert after["outstanding"] == 0
    assert after["failed"] == 0
    assert after["oldest_pending_age_seconds"] is None
    runtime.close()
