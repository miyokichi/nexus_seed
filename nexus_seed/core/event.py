"""Event — the immutable record of "something happened".

Events are the only way information enters NEXUS SEED.  They are treated as an
append-only history: once created an :class:`Event` is never mutated (the
dataclass is frozen).  Causal chains between events are tracked with
``correlation_id`` (one logical piece of work) and ``causation_id`` (the event
that directly caused this one).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Event:
    """An immutable fact about the world.

    Attributes:
        type: What kind of thing happened (e.g. ``"measurement_completed"``).
        source: Where the event originated (e.g. ``"metrology"``).
        payload: Arbitrary structured data describing the event.
        id: Unique identifier, generated automatically.
        occurred_at: When the event happened (UTC).
        correlation_id: Groups a whole unit of work across many events.
        causation_id: The id of the event that directly caused this one.
        ingress_receipt_id: Set only when this event entered from *outside*
            through the Phase 3D ingress boundary (Invariant 28).  Events a
            process emits leave it ``None``, so the field doubles as the answer
            to "did the world tell us this, or did we conclude it?".
    """

    type: str
    source: str
    payload: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=utcnow)
    correlation_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    ingress_receipt_id: uuid.UUID | None = None

    @property
    def is_external(self) -> bool:
        """Whether this event came from the outside world via ingress."""
        return self.ingress_receipt_id is not None
