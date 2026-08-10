"""State — what NEXUS SEED currently knows about the world.

State is not a plain key-value store: it is a *projection* of how NEXUS SEED
currently believes the world to be, and every fact keeps its value, version,
confidence, time and provenance.

Phase 2B stores State as two logical structures (see
:mod:`nexus_seed.storage.state_store`):

* ``world_state_history`` — the append-only source of truth (every version);
* ``world_state_current`` — a rebuildable projection of the latest version.

This module holds the in-memory models:

* :class:`StateEntry` — one current fact (with provenance).
* :class:`StateHistoryEntry` — one historical version of a fact.
* :class:`StateChange` — a *requested* write returned by a process.
* :class:`StateView` — the buffered facade handlers write through.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .event import utcnow


@dataclass
class StateEntry:
    """A single current fact about the world (a row of ``world_state_current``).

    Attributes:
        entity: The thing the fact is about (e.g. ``"D1_CD"``).
        attribute: Which property of the entity (e.g. ``"target"``).
        value: The value of that property.
        version: Which historical version is current.
        source_event: The event that produced this value, if known.
        observation_id: The observation behind this value, if any.
        state_delta_id: The delta that applied this value, if any.
        created_by_process_id: The process that wrote this value.
        confidence: Belief in this value.
        history_id: The history row this projection points at.
        updated_at: When this fact was last written (UTC).
    """

    entity: str
    attribute: str
    value: Any
    version: int = 1
    source_event: uuid.UUID | None = None
    observation_id: uuid.UUID | None = None
    state_delta_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    confidence: float = 1.0
    history_id: uuid.UUID | None = None
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class StateHistoryEntry:
    """One historical version of a fact (a row of ``world_state_history``).

    ``valid_to`` is ``None`` while this version is current, and is closed when a
    newer version is written — the row's value is never destroyed.
    """

    entity: str
    attribute: str
    value: Any
    version: int
    valid_from: datetime
    valid_to: datetime | None = None
    source_event: uuid.UUID | None = None
    observation_id: uuid.UUID | None = None
    state_delta_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    confidence: float = 1.0
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class StateChange:
    """A requested change to world state, returned by a process handler.

    The runtime applies a batch of these inside the process's atomic
    transaction; a handler never writes state directly.  Provenance travels
    with the change so history rows record why the value is believed.
    """

    entity: str
    attribute: str
    value: Any
    source_event: uuid.UUID | None = None
    observation_id: uuid.UUID | None = None
    state_delta_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    confidence: float = 1.0


class StateView:
    """A buffered view of world state passed to a handler as ``ctx.state``.

    Reads fall through to the underlying store but reflect writes staged during
    this activation; writes are recorded as :class:`StateChange` objects (in
    order) rather than committed immediately.  The executor collects
    :attr:`changes` and applies them atomically with the rest of the result.
    """

    def __init__(
        self, store: Any, *, created_by_process_id: uuid.UUID | None = None
    ) -> None:
        self._store = store
        self._created_by = created_by_process_id
        self._staged: dict[tuple[str, str], StateChange] = {}
        self.changes: list[StateChange] = []

    def get(self, entity: str, attribute: str, default: Any = None) -> Any:
        """Return the staged value if written this activation, else the stored value."""
        key = (entity, attribute)
        if key in self._staged:
            return self._staged[key].value
        return self._store.get(entity, attribute, default)

    def get_entry(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the committed current :class:`StateEntry` (staged not reflected)."""
        return self._store.get_current(entity, attribute)

    def set(
        self,
        entity: str,
        attribute: str,
        value: Any,
        *,
        source_event: uuid.UUID | None = None,
        observation_id: uuid.UUID | None = None,
        state_delta_id: uuid.UUID | None = None,
        created_by_process_id: uuid.UUID | None = None,
        confidence: float = 1.0,
    ) -> StateChange:
        """Stage a write (with provenance); applied when the activation commits."""
        change = StateChange(
            entity=entity,
            attribute=attribute,
            value=value,
            source_event=source_event,
            observation_id=observation_id,
            state_delta_id=state_delta_id,
            created_by_process_id=created_by_process_id or self._created_by,
            confidence=confidence,
        )
        self._staged[(entity, attribute)] = change
        self.changes.append(change)
        return change
