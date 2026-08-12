"""Observation — what a Process *read* from a raw Event.

An Observation is domain data (not a Runtime primitive).  It records a
*semantic reading* of an event, kept distinct from the world-state change that
reading implies:

    Event       = what happened
    Observation = what a process read from the event
    StateDelta  = what it concluded changed about the world
    World State = how the world is currently believed to be

The model is intentionally not 1:1 with events — one event may yield several
observations later (Phase 3+), so nothing here assumes a single observation per
event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.event import utcnow


@dataclass
class Observation:
    """A semantic reading of a raw event produced by a process.

    Attributes:
        subject: What the observation is about (e.g. ``"D1_CD"``).
        predicate: What was read (e.g. ``"target_changed"``).
        extracted: Structured data pulled from the event.
        source_event_id: The raw event this reading came from.
        created_by_process_id: The process instance that made the reading.
        confidence: How sure the reading is (``1.0`` for deterministic parsers).
        id: Unique identifier.
        created_at: When the observation was made (UTC).
    """

    subject: str
    predicate: str
    extracted: dict[str, Any] = field(default_factory=dict)
    source_event_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    confidence: float = 1.0
    proposal_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
