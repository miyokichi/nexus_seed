"""StateDelta — a proposed semantic change to world state.

A StateDelta is domain data (not a Runtime primitive).  It says "based on an
observation, this ``entity.attribute`` should change from ``old_value`` to
``new_value``".  Applying it is a separate step (the ``apply_state_delta``
process), which validates it against current state before committing.

Deltas are never 1:1-locked to observations: one observation may yield several
deltas later, so this model does not assume a single delta per observation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.event import utcnow


@dataclass
class StateDelta:
    """A proposed change to one world-state fact.

    Attributes:
        entity: The entity whose fact changes (e.g. ``"D1_CD"``).
        attribute: The attribute that changes (e.g. ``"target"``).
        old_value: The value the delta expects to replace (``None`` = new fact).
        new_value: The value to set.
        source_event_id: The raw event that ultimately caused this delta.
        observation_id: The observation this delta was derived from.
        created_by_process_id: The process instance that proposed the delta.
        confidence: How sure the proposal is.
        reason: Optional human-readable justification.
        valid_from: When the new value takes effect.
        id: Unique identifier.
        created_at: When the delta was created (UTC).
    """

    entity: str
    attribute: str
    old_value: Any
    new_value: Any
    source_event_id: uuid.UUID | None = None
    observation_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    confidence: float = 1.0
    reason: str | None = None
    valid_from: datetime = field(default_factory=utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


class StateConflict(Exception):
    """Domain error: a delta's ``old_value`` disagrees with current state.

    This is a *domain* error raised/handled inside processes — never a Runtime
    error.  The runtime just sees a failed process result.
    """

    def __init__(self, entity: str, attribute: str, expected: Any, actual: Any) -> None:
        self.entity = entity
        self.attribute = attribute
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"state conflict on {entity}.{attribute}: "
            f"delta expected old_value={expected!r} but current={actual!r}"
        )
