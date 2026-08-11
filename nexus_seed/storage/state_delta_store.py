"""Persistence for :class:`~nexus_seed.world.state_delta.StateDelta`.

Deltas are kept even after being applied, so the chain
Event -> Observation -> StateDelta -> State version stays fully traceable.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..world.state_delta import StateDelta
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class StateDeltaStore:
    """Stores state deltas (never deleted once applied)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, delta: StateDelta) -> StateDelta:
        """Persist a state delta (insert only)."""
        self.db.execute(
            """
            INSERT INTO state_deltas
                (id, entity, attribute, old_value, new_value, source_event_id,
                 observation_id, created_by_process_id, confidence, reason,
                 valid_from, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(delta.id),
                delta.entity,
                delta.attribute,
                dumps(delta.old_value),
                dumps(delta.new_value),
                str(delta.source_event_id) if delta.source_event_id else None,
                str(delta.observation_id) if delta.observation_id else None,
                str(delta.created_by_process_id) if delta.created_by_process_id else None,
                delta.confidence,
                delta.reason,
                delta.valid_from.isoformat(),
                delta.created_at.isoformat(),
            ),
        )
        return delta

    def get(self, delta_id: uuid.UUID) -> StateDelta | None:
        """Return the delta with ``delta_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM state_deltas WHERE id = ?", (str(delta_id),)
        )
        return self._row(row) if row else None

    def for_entity(self, entity: str, attribute: str) -> list[StateDelta]:
        """Return all deltas for a fact, oldest first."""
        rows = self.db.query(
            "SELECT * FROM state_deltas WHERE entity = ? AND attribute = ? "
            "ORDER BY created_at ASC",
            (entity, attribute),
        )
        return [self._row(r) for r in rows]

    def all(self) -> list[StateDelta]:
        """Return all deltas in creation order."""
        rows = self.db.query("SELECT * FROM state_deltas ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    def recent(self, n: int) -> list[StateDelta]:
        """Return the ``n`` most recent deltas, oldest first."""
        rows = self.db.query(
            "SELECT * FROM state_deltas ORDER BY created_at DESC LIMIT ?", (n,)
        )
        return [self._row(r) for r in reversed(rows)]

    def by_entity(self, entity: str) -> list[StateDelta]:
        """Return all deltas for ``entity`` (any attribute), oldest first."""
        rows = self.db.query(
            "SELECT * FROM state_deltas WHERE entity = ? ORDER BY created_at ASC",
            (entity,),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> StateDelta:
        return StateDelta(
            entity=row["entity"],
            attribute=row["attribute"],
            old_value=loads(row["old_value"]),
            new_value=loads(row["new_value"]),
            source_event_id=_uuid(row["source_event_id"]),
            observation_id=_uuid(row["observation_id"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            confidence=row["confidence"],
            reason=row["reason"],
            valid_from=datetime.fromisoformat(row["valid_from"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
