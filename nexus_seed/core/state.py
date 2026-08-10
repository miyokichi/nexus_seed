"""State — what NEXUS SEED currently knows about the world.

Phase 1 keeps State as a flat set of ``(entity, attribute) -> value`` facts,
each carrying a ``version`` and the ``source_event`` that last wrote it.  This
is stored in SQLite but modelled so a future move to a graph database is not
blocked: every fact is already an ``entity / attribute / value`` triple.

    entity = "D1_CD",  attribute = "target",  value = 45

Phase 2A adds :class:`StateChange` (a *requested* write returned by a process,
applied atomically by the runtime) and :class:`StateView` (a buffered facade
handlers use so their writes are staged, not committed mid-activation).
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


@dataclass
class StateChange:
    """A requested change to world state, returned by a process handler.

    The runtime applies a batch of these inside the process's atomic
    transaction; a handler never writes state directly.
    """

    entity: str
    attribute: str
    value: Any
    source_event: uuid.UUID | None = None


class StateView:
    """A buffered view of world state passed to a handler as ``ctx.state``.

    Reads fall through to the underlying store but reflect writes staged during
    this activation; writes are recorded as :class:`StateChange` objects (in
    order) rather than committed immediately.  The executor collects
    :attr:`changes` and applies them atomically with the rest of the result.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._staged: dict[tuple[str, str], StateChange] = {}
        self.changes: list[StateChange] = []

    def get(self, entity: str, attribute: str, default: Any = None) -> Any:
        """Return the staged value if written this activation, else the stored value."""
        key = (entity, attribute)
        if key in self._staged:
            return self._staged[key].value
        return self._store.get(entity, attribute, default)

    def get_entry(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the committed :class:`StateEntry` (staged writes not reflected)."""
        return self._store.get_entry(entity, attribute)

    def set(
        self,
        entity: str,
        attribute: str,
        value: Any,
        *,
        source_event: uuid.UUID | None = None,
    ) -> StateChange:
        """Stage a write; it is applied when the activation commits."""
        change = StateChange(entity, attribute, value, source_event)
        self._staged[(entity, attribute)] = change
        self.changes.append(change)
        return change
