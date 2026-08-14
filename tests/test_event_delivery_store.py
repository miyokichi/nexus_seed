"""EventDeliveryStore (spec §46, §47): the obligation ledger.

The UNIQUE constraint on ``event_id`` is what turns "every persisted event owes
exactly one routing attempt" from a convention into a database fact.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from nexus_seed.core.event import Event
from nexus_seed.delivery.models import EventDeliveryStatus
from nexus_seed.storage.database import Database
from nexus_seed.storage.event_delivery_store import EventDeliveryStore
from nexus_seed.storage.event_store import EventStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stores(tmp_path, name="d.db"):
    db = Database(tmp_path / name)
    deliveries = EventDeliveryStore(db)
    return db, deliveries, EventStore(db, deliveries)


def append(events, n=1, event_type="ping"):
    made = []
    for i in range(n):
        event = Event(event_type, "test", {"i": i})
        events.append(event)
        made.append(event)
    return made


# --- creation --------------------------------------------------------------


def test_appending_an_event_records_its_obligation(tmp_path):
    """Spec §8: the two commit together, so neither can exist alone."""
    db, deliveries, events = stores(tmp_path)
    (event,) = append(events)

    delivery = deliveries.get(event.id)
    assert delivery is not None
    assert delivery.status is EventDeliveryStatus.PENDING
    assert delivery.attempt_count == 0
    db.close()


def test_an_event_store_without_a_delivery_store_still_works(tmp_path):
    """The dependency is optional, so the store stays usable in isolation."""
    db = Database(tmp_path / "d.db")
    events = EventStore(db)
    event = Event("ping", "test", {})
    events.append(event)
    assert events.get(event.id) is not None
    db.close()


def test_creating_the_same_obligation_twice_is_ignored(tmp_path):
    db, deliveries, events = stores(tmp_path)
    (event,) = append(events)
    first = deliveries.get(event.id)

    deliveries.create_for_event(event.id)

    assert deliveries.get(event.id).id == first.id
    assert len(deliveries.all()) == 1
    db.close()


def test_a_delivery_can_be_created_already_delivered(tmp_path):
    """Used by the legacy backfill, which must not replay history."""
    db, deliveries, events = stores(tmp_path)
    event_id = uuid.uuid4()
    deliveries.create_for_event(event_id, status=EventDeliveryStatus.DELIVERED)

    delivery = deliveries.get(event_id)
    assert delivery.status is EventDeliveryStatus.DELIVERED
    assert delivery.delivered_at is not None
    db.close()


# --- dispatch selection ----------------------------------------------------


def test_dispatchable_deliveries_come_back_in_event_order(tmp_path):
    db, deliveries, events = stores(tmp_path)
    made = append(events, 3)

    dispatchable = deliveries.list_dispatchable(NOW)
    assert [d.event_id for d in dispatchable] == [e.id for e in made]
    db.close()


def test_a_retry_wait_delivery_is_skipped_until_its_time(tmp_path):
    """Spec §31: one waiting event must not hold up the others."""
    db, deliveries, events = stores(tmp_path)
    first, second = append(events, 2)
    deliveries.mark_retry(first.id, "boom", NOW + timedelta(seconds=60))

    dispatchable = deliveries.list_dispatchable(NOW)
    assert [d.event_id for d in dispatchable] == [second.id]

    later = deliveries.list_dispatchable(NOW + timedelta(seconds=60))
    assert sorted(str(d.event_id) for d in later) == sorted(
        [str(first.id), str(second.id)]
    )
    db.close()


def test_delivered_and_failed_are_never_dispatchable(tmp_path):
    db, deliveries, events = stores(tmp_path)
    delivered, failed, pending = append(events, 3)
    deliveries.mark_delivered(delivered.id)
    deliveries.mark_failed(failed.id, "gave up")

    assert [d.event_id for d in deliveries.list_dispatchable(NOW)] == [pending.id]
    db.close()


def test_the_batch_limit_is_honoured(tmp_path):
    db, deliveries, events = stores(tmp_path)
    append(events, 5)
    assert len(deliveries.list_dispatchable(NOW, limit=2)) == 2
    db.close()


# --- transitions -----------------------------------------------------------


def test_marking_delivering_counts_the_attempt(tmp_path):
    db, deliveries, events = stores(tmp_path)
    (event,) = append(events)

    deliveries.mark_delivering(event.id)
    first = deliveries.get(event.id)
    assert first.status is EventDeliveryStatus.DELIVERING
    assert first.attempt_count == 1

    deliveries.mark_delivering(event.id)
    assert deliveries.get(event.id).attempt_count == 2
    db.close()


def test_marking_delivered_clears_the_error_and_stamps_the_time(tmp_path):
    db, deliveries, events = stores(tmp_path)
    (event,) = append(events)
    deliveries.mark_retry(event.id, "boom", NOW)

    deliveries.mark_delivered(event.id)
    delivery = deliveries.get(event.id)

    assert delivery.status is EventDeliveryStatus.DELIVERED
    assert delivery.last_error is None
    assert delivery.next_attempt_at is None
    assert delivery.delivered_at is not None
    db.close()


def test_marking_retry_records_why_and_when(tmp_path):
    db, deliveries, events = stores(tmp_path)
    (event,) = append(events)

    deliveries.mark_retry(event.id, "router exploded", NOW + timedelta(seconds=8))
    delivery = deliveries.get(event.id)

    assert delivery.status is EventDeliveryStatus.RETRY_WAIT
    assert delivery.last_error == "router exploded"
    assert delivery.next_attempt_at == NOW + timedelta(seconds=8)
    db.close()


def test_stale_delivering_records_are_recovered(tmp_path):
    """Spec §20: a claim that never completed is safe to re-attempt."""
    db, deliveries, events = stores(tmp_path)
    claimed, untouched = append(events, 2)
    deliveries.mark_delivering(claimed.id)

    recovered = deliveries.recover_stale_delivering()

    assert recovered == [claimed.id]
    assert deliveries.get(claimed.id).status is EventDeliveryStatus.PENDING
    assert deliveries.get(untouched.id).status is EventDeliveryStatus.PENDING
    # The attempt is still counted — the try really happened.
    assert deliveries.get(claimed.id).attempt_count == 1
    db.close()


def test_recovery_is_a_no_op_when_nothing_was_claimed(tmp_path):
    db, deliveries, events = stores(tmp_path)
    append(events, 2)
    assert deliveries.recover_stale_delivering() == []
    db.close()


# --- health ----------------------------------------------------------------


def test_counts_and_oldest_pending(tmp_path):
    db, deliveries, events = stores(tmp_path)
    a, b, c = append(events, 3)
    deliveries.mark_delivered(a.id)
    deliveries.mark_retry(b.id, "boom", NOW)

    assert deliveries.count_pending() == 2  # retry_wait + pending
    assert deliveries.oldest_pending_at() is not None
    assert [d.event_id for d in deliveries.by_status(EventDeliveryStatus.DELIVERED)] == [
        a.id
    ]

    deliveries.mark_delivered(b.id)
    deliveries.mark_delivered(c.id)
    assert deliveries.count_pending() == 0
    assert deliveries.oldest_pending_at() is None
    db.close()


# --- backfill --------------------------------------------------------------


def test_backfill_gives_orphan_events_a_delivered_record(tmp_path):
    """Spec §50: pre-3F events must not be replayed."""
    db, deliveries, events = stores(tmp_path)
    legacy = Event("ping", "legacy", {})
    events._insert(legacy)  # persisted the pre-3F way: no obligation recorded
    (modern,) = append(events)

    count = deliveries.backfill_missing()

    assert count == 1
    assert deliveries.get(legacy.id).status is EventDeliveryStatus.DELIVERED
    assert deliveries.get(modern.id).status is EventDeliveryStatus.PENDING
    # Running it again finds nothing left to do.
    assert deliveries.backfill_missing() == 0
    db.close()
