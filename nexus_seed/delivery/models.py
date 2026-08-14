"""EventDelivery — the record that an Event still has to be thought about.

Phase 3F separates two things that had been conflated since Phase 1::

    Event persistence   the fact is durable
    Event delivery      somebody has been given the chance to react to it

Storing an event is not enough.  An event-driven system is only honest if every
persisted event is *guaranteed* to reach the router eventually — otherwise a
crash at the wrong moment leaves a fact in the database that nothing will ever
act on, and (worse) that upstream deduplication will refuse to re-deliver.

That was a real hole after Phase 3E: the ingress boundary committed an event
with its receipt, and the observer activation that was going to route it could
fail afterwards.  The source key was consumed, so no re-poll would bring it
back.  A delivery record closes it: the event is durable, the *obligation* to
route it is durable too, and a restart picks it up.

This is Runtime/infrastructure data, not a core primitive and not domain data —
it says nothing about the world, only about our own bookkeeping.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.event import utcnow


class EventDeliveryStatus(str, Enum):
    """Lifecycle of one event's obligation to be routed.

    ``DELIVERING`` is committed on its own before routing starts, so a crash
    mid-route leaves a visible signature that startup recovery can return to
    ``PENDING``.  ``FAILED`` is deliberately rare — a routing failure is
    normally transient, and Phase 3F prefers retrying forever to forgetting.
    """

    PENDING = "PENDING"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"


#: Statuses that still owe the system a routing attempt.
OUTSTANDING = (
    EventDeliveryStatus.PENDING,
    EventDeliveryStatus.DELIVERING,
    EventDeliveryStatus.RETRY_WAIT,
)


@dataclass
class EventDelivery:
    """The durable obligation to route one Event.

    Attributes:
        event_id: The event owed a routing attempt (unique — Phase 3F tracks
            delivery per event, not per subscriber; splitting that out would be
            a later change and is not needed yet).
        status: Where this obligation stands.
        attempt_count: How many routing attempts have been made.
        next_attempt_at: When a RETRY_WAIT delivery becomes dispatchable again.
        last_error: Why the most recent attempt failed.
        delivered_at: When the routing result was committed.
    """

    event_id: uuid.UUID
    status: EventDeliveryStatus = EventDeliveryStatus.PENDING
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    delivered_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def outstanding(self) -> bool:
        """Whether this event still owes the system a routing attempt."""
        return self.status in OUTSTANDING

    @property
    def settled(self) -> bool:
        """Whether this delivery will not be attempted again on its own."""
        return self.status in (
            EventDeliveryStatus.DELIVERED,
            EventDeliveryStatus.FAILED,
        )


def backoff_seconds(attempt: int, *, base: float = 2.0, maximum: float = 300.0) -> float:
    """Exponential backoff for a failed routing attempt.

    Same shape as the Phase 2A process retry: doubling, capped.  The cap
    matters more than the curve — an event must never back off so far that it
    is effectively forgotten.
    """
    if attempt <= 1:
        return 1.0
    return min(base ** (attempt - 1), maximum)
