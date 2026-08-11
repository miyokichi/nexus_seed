"""Append-only persistence for :class:`~nexus_seed.core.event.Event`."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.event import Event
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class EventStore:
    """Stores events append-only and reads them back by id/type/correlation."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, event: Event) -> Event:
        """Persist ``event``.  Events are never updated once written."""
        self.db.execute(
            """
            INSERT INTO events
                (id, type, source, payload, occurred_at, correlation_id, causation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.id),
                event.type,
                event.source,
                dumps(event.payload),
                event.occurred_at.isoformat(),
                str(event.correlation_id) if event.correlation_id else None,
                str(event.causation_id) if event.causation_id else None,
            ),
        )
        return event

    def get(self, event_id: uuid.UUID) -> Event | None:
        """Return the event with ``event_id``, or ``None``."""
        row = self.db.query_one("SELECT * FROM events WHERE id = ?", (str(event_id),))
        return self._row_to_event(row) if row else None

    def all(self) -> list[Event]:
        """Return all events in insertion order."""
        rows = self.db.query("SELECT * FROM events ORDER BY seq ASC")
        return [self._row_to_event(r) for r in rows]

    def by_type(self, event_type: str) -> list[Event]:
        """Return all events of ``event_type`` in insertion order."""
        rows = self.db.query(
            "SELECT * FROM events WHERE type = ? ORDER BY seq ASC", (event_type,)
        )
        return [self._row_to_event(r) for r in rows]

    def recent(self, n: int) -> list[Event]:
        """Return the ``n`` most recently appended events, oldest first."""
        rows = self.db.query(
            "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (n,)
        )
        return [self._row_to_event(r) for r in reversed(rows)]

    def referencing_entity(self, entity: str) -> list[Event]:
        """Return events whose payload references ``entity`` (deterministic scan)."""
        result = []
        for row in self.db.query("SELECT * FROM events ORDER BY seq ASC"):
            event = self._row_to_event(row)
            if entity in [v for v in event.payload.values() if isinstance(v, str)]:
                result.append(event)
        return result

    def by_correlation(self, correlation_id: uuid.UUID) -> list[Event]:
        """Return all events sharing ``correlation_id`` in insertion order."""
        rows = self.db.query(
            "SELECT * FROM events WHERE correlation_id = ? ORDER BY seq ASC",
            (str(correlation_id),),
        )
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row) -> Event:
        return Event(
            type=row["type"],
            source=row["source"],
            payload=loads(row["payload"]),
            id=uuid.UUID(row["id"]),
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            correlation_id=_uuid(row["correlation_id"]),
            causation_id=_uuid(row["causation_id"]),
        )
