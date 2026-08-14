"""EventDelivery model + backoff (spec §6, §7, §19).

The model exists to make one distinction explicit: *the event is stored* and
*somebody has been given the chance to react to it* are different facts, and
only tracking the second makes "no event is ever forgotten" checkable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from nexus_seed.delivery.dispatcher import dispatchable_at
from nexus_seed.delivery.models import (
    OUTSTANDING,
    EventDelivery,
    EventDeliveryStatus,
    backoff_seconds,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_a_new_delivery_starts_pending_and_unattempted():
    delivery = EventDelivery(event_id=uuid.uuid4())
    assert delivery.status is EventDeliveryStatus.PENDING
    assert delivery.attempt_count == 0
    assert delivery.next_attempt_at is None
    assert delivery.delivered_at is None


def test_outstanding_covers_every_state_that_still_owes_an_attempt():
    assert set(OUTSTANDING) == {
        EventDeliveryStatus.PENDING,
        EventDeliveryStatus.DELIVERING,
        EventDeliveryStatus.RETRY_WAIT,
    }
    for status in OUTSTANDING:
        assert EventDelivery(event_id=uuid.uuid4(), status=status).outstanding


def test_settled_states_are_not_outstanding():
    for status in (EventDeliveryStatus.DELIVERED, EventDeliveryStatus.FAILED):
        delivery = EventDelivery(event_id=uuid.uuid4(), status=status)
        assert delivery.settled
        assert not delivery.outstanding


def test_backoff_doubles_and_is_capped():
    """An event must never back off so far that it is effectively forgotten."""
    assert backoff_seconds(1) == 1.0
    assert backoff_seconds(2) == 2.0
    assert backoff_seconds(3) == 4.0
    assert backoff_seconds(5) == 16.0
    assert backoff_seconds(50) == 300.0
    assert backoff_seconds(0) == 1.0


def test_a_pending_delivery_is_always_dispatchable():
    assert dispatchable_at(EventDelivery(event_id=uuid.uuid4()), NOW)


def test_a_retry_wait_delivery_waits_for_its_backoff():
    delivery = EventDelivery(
        event_id=uuid.uuid4(),
        status=EventDeliveryStatus.RETRY_WAIT,
        next_attempt_at=NOW + timedelta(seconds=30),
    )
    assert not dispatchable_at(delivery, NOW)
    assert dispatchable_at(delivery, NOW + timedelta(seconds=30))
    assert dispatchable_at(delivery, NOW + timedelta(seconds=31))


def test_settled_deliveries_are_never_dispatchable():
    """No replay: a delivered event is not routed again (spec §28)."""
    for status in (EventDeliveryStatus.DELIVERED, EventDeliveryStatus.FAILED):
        delivery = EventDelivery(event_id=uuid.uuid4(), status=status)
        assert not dispatchable_at(delivery, NOW)


def test_a_delivering_delivery_is_not_picked_up_by_a_concurrent_sweep():
    """It is claimed; recovery, not a second sweep, is what frees it."""
    delivery = EventDelivery(
        event_id=uuid.uuid4(), status=EventDeliveryStatus.DELIVERING
    )
    assert not dispatchable_at(delivery, NOW)
    assert delivery.outstanding
