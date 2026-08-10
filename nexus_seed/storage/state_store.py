"""Persistence for world State as ``(entity, attribute) -> value`` facts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ..core.event import utcnow
from ..core.state import StateEntry
from .database import Database, dumps, loads

_MISSING = object()


class StateStore:
    """Stores and retrieves world-state facts, tracking version and source."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def set(
        self,
        entity: str,
        attribute: str,
        value: Any,
        *,
        source_event: uuid.UUID | None = None,
    ) -> StateEntry:
        """Write a fact, bumping its ``version`` if it already existed."""
        existing = self.get_entry(entity, attribute)
        version = (existing.version + 1) if existing else 1
        now = utcnow()
        self.db.execute(
            """
            INSERT INTO world_state
                (entity, attribute, value, version, source_event, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity, attribute) DO UPDATE SET
                value = excluded.value,
                version = excluded.version,
                source_event = excluded.source_event,
                updated_at = excluded.updated_at
            """,
            (
                entity,
                attribute,
                dumps(value),
                version,
                str(source_event) if source_event else None,
                now.isoformat(),
            ),
        )
        return StateEntry(
            entity=entity,
            attribute=attribute,
            value=value,
            version=version,
            source_event=source_event,
            updated_at=now,
        )

    def get(self, entity: str, attribute: str, default: Any = None) -> Any:
        """Return the value of a fact, or ``default`` if it is not set."""
        entry = self.get_entry(entity, attribute)
        return entry.value if entry is not None else default

    def get_entry(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the full :class:`StateEntry`, or ``None`` if not set."""
        row = self.db.query_one(
            "SELECT * FROM world_state WHERE entity = ? AND attribute = ?",
            (entity, attribute),
        )
        return self._row_to_entry(row) if row else None

    def all_entries(self) -> list[StateEntry]:
        """Return every stored fact."""
        rows = self.db.query(
            "SELECT * FROM world_state ORDER BY entity ASC, attribute ASC"
        )
        return [self._row_to_entry(r) for r in rows]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return all facts as a nested ``{entity: {attribute: value}}`` dict."""
        snapshot: dict[str, dict[str, Any]] = {}
        for entry in self.all_entries():
            snapshot.setdefault(entry.entity, {})[entry.attribute] = entry.value
        return snapshot

    @staticmethod
    def _row_to_entry(row) -> StateEntry:
        return StateEntry(
            entity=row["entity"],
            attribute=row["attribute"],
            value=loads(row["value"]),
            version=row["version"],
            source_event=uuid.UUID(row["source_event"]) if row["source_event"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
