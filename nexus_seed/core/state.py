"""State — what NEXUS SEED currently knows about the world.

Phase 1 keeps State as a flat set of ``(entity, attribute) -> value`` facts,
each carrying a ``version`` and the ``source_event`` that last wrote it.  This
is stored in SQLite but modelled so a future move to a graph database is not
blocked: every fact is already an ``entity / attribute / value`` triple.

    entity = "D1_CD",  attribute = "target",  value = 45
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .event import utcnow


@dataclass
class StateEntry:
    """A single fact about the world.

    Attributes:
        entity: The thing the fact is about (e.g. ``"D1_CD"``).
        attribute: Which property of the entity (e.g. ``"target"``).
        value: The value of that property.
        version: Monotonically increasing revision of this fact.
        source_event: The event that produced this value, if known.
        updated_at: When this fact was last written (UTC).
    """

    entity: str
    attribute: str
    value: Any
    version: int = 1
    source_event: uuid.UUID | None = None
    updated_at: datetime = field(default_factory=utcnow)
