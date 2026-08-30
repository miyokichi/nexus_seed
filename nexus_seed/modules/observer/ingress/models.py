"""Ingress domain models — how an outside occurrence becomes an Event.

Phase 3D adds the stage *before* perception.  Everything from ``Raw Event``
onwards already existed; what was missing was a disciplined way to get there
from the world::

    External source -> Adapter -> IngressEnvelope -> validation -> dedup
                    -> IngressReceipt + Event

Nothing here is a core primitive (the six stay fixed).  These are domain models
under ``ingress/``, like ``world/``, ``work/``, ``intelligence/`` and
``actions/`` before them.

The load-bearing idea is :attr:`IngressEnvelope.source_event_key`.  An
``Event.id`` is *our* name for something; a ``source_event_key`` is the outside
world's name for it (Invariant 30).  Keeping them separate is what lets a
webhook redelivery, a re-poll, a re-scan and a runtime restart all converge on
one logical Event (Invariant 31).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ....core.event import utcnow


class DuplicateIngress(Exception):
    """This ``(adapter_id, source_event_key)`` has already been ingested.

    Raised by the receipt store when the UNIQUE constraint refuses a second
    row.  It lives here rather than in ``storage/`` because "we already know
    about this external occurrence" is a domain fact, not a database detail —
    and keeping it here lets the store and the service share it without an
    import cycle.
    """


class IngressStatus(str, Enum):
    """Outcome of offering one envelope to the ingress boundary.

    ``RECEIVED`` is the transient state a receipt is built in; a stored receipt
    is always one of the other three.  ``REJECTED`` is only persisted when the
    envelope carried enough identity to be keyed — an envelope with no
    ``adapter_id``/``source_event_key`` cannot be recorded without breaking the
    very uniqueness that makes the table meaningful.
    """

    RECEIVED = "RECEIVED"
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


@dataclass
class IngressEnvelope:
    """One external occurrence, as an Adapter understood it.

    An envelope is *not yet* an Event: it has not been validated, deduplicated
    or persisted, and it may turn out to describe something already known.

    Attributes:
        adapter_id: Which adapter observed it (``"manual"``, ``"local_file"``…).
        source_type: The kind of source (``"webhook"``, ``"file"``, ``"cli"``).
        source_event_key: The *outside world's* identity for this occurrence.
            Chosen by the adapter (Invariant 30 / spec §17), never by the
            ingress service, because only the adapter knows what makes two
            observations "the same thing" for its source.
        event_type: The NEXUS Event type this becomes.
        payload: The Event payload.  Adapters do not interpret meaning
            (Invariant 34) — this is a faithful description, not a conclusion.
        observed_at: When the adapter saw it.
        source_cursor: The source's position at this item, if the adapter is a
            pull/watch adapter that keeps a checkpoint.
        metadata: Adapter-specific extras kept for audit.
    """

    adapter_id: str
    source_type: str
    source_event_key: str
    event_type: str
    payload: dict = field(default_factory=dict)
    observed_at: datetime = field(default_factory=utcnow)
    source_cursor: str | None = None
    metadata: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def identity(self) -> tuple[str, str]:
        """The ``(adapter_id, source_event_key)`` pair that dedup keys on."""
        return (self.adapter_id, self.source_event_key)


@dataclass
class IngressReceipt:
    """The durable record that an external occurrence was taken in.

    One receipt per *logical external event*, enforced by a UNIQUE index on
    ``(adapter_id, source_event_key)``.  A redelivery finds the existing receipt
    and stops there — no second Event, and therefore no second interpretation,
    no second WorkRequirement and no second action.
    """

    adapter_id: str
    source_type: str
    source_event_key: str
    event_type: str
    payload: dict = field(default_factory=dict)
    source_cursor: str | None = None
    metadata: dict = field(default_factory=dict)
    observed_at: datetime = field(default_factory=utcnow)
    received_at: datetime = field(default_factory=utcnow)
    event_id: uuid.UUID | None = None
    status: IngressStatus = IngressStatus.RECEIVED
    reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @classmethod
    def from_envelope(
        cls, envelope: IngressEnvelope, *, status: IngressStatus = IngressStatus.RECEIVED
    ) -> "IngressReceipt":
        """Build a receipt mirroring ``envelope``."""
        return cls(
            adapter_id=envelope.adapter_id,
            source_type=envelope.source_type,
            source_event_key=envelope.source_event_key,
            event_type=envelope.event_type,
            payload=dict(envelope.payload),
            source_cursor=envelope.source_cursor,
            metadata=dict(envelope.metadata),
            observed_at=envelope.observed_at,
            status=status,
        )


@dataclass
class AdapterCheckpoint:
    """How far a pull/watch adapter has observed one stream.

    Explicitly **not** a Continuation (Invariant 33).  A Continuation is a
    *process's* future — where our own execution resumes.  A Checkpoint is the
    *world's* past — how much of an external source we have already looked at.
    Conflating them would make a restart either replay work or lose observations.

    Attributes:
        adapter_id: Owning adapter.
        stream_key: Which stream within that adapter (a file path, a queue
            name, an object id — the adapter decides).
        cursor: The opaque position/fingerprint at that point.
        metadata: Adapter-specific extras.
    """

    adapter_id: str
    stream_key: str
    cursor: str | None = None
    metadata: dict = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class IngressResult:
    """What the ingress boundary did with one envelope.

    Attributes:
        status: ACCEPTED (a new Event exists), DUPLICATE (one already did) or
            REJECTED (the envelope was unusable).
        receipt: The stored receipt, when one exists.
        event: The Event — the newly created one, or, for a duplicate, the one
            the original delivery produced.
        reasons: Why a REJECTED envelope was refused.
    """

    status: IngressStatus
    receipt: IngressReceipt | None = None
    event: object | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """Whether this delivery created a new Event."""
        return self.status is IngressStatus.ACCEPTED

    @property
    def duplicate(self) -> bool:
        """Whether this delivery described an occurrence already ingested."""
        return self.status is IngressStatus.DUPLICATE
