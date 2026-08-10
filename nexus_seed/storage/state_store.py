"""World-state persistence: append-only history + a rebuildable current view.

``world_state_history`` is the source of truth — every version of every fact,
never destructively updated (only ``valid_to`` is closed).  ``world_state_current``
is a projection of the latest version for fast reads, and can be fully rebuilt
from history with :meth:`rebuild_current_state`.

All writes go through :meth:`set`, which — inside the caller's transaction —
closes the previous history row, appends the new version and updates the
projection, so state application stays atomic (Phase 2A).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ..core.event import utcnow
from ..core.state import StateEntry, StateHistoryEntry
from .database import Database, dumps, loads

_MISSING = object()


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class StateStore:
    """Reads and writes world state as versioned, provenance-bearing facts."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- writes ------------------------------------------------------------

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
        valid_from: datetime | None = None,
    ) -> StateEntry:
        """Append a new version of ``entity.attribute`` and update the projection."""
        now = utcnow()
        valid_from = valid_from or now
        current = self.get_current(entity, attribute)
        version = (current.version + 1) if current else 1

        # Close the previously-open history row (its value is preserved).
        self.db.execute(
            "UPDATE world_state_history SET valid_to = ? "
            "WHERE entity = ? AND attribute = ? AND valid_to IS NULL",
            (valid_from.isoformat(), entity, attribute),
        )

        history = StateHistoryEntry(
            entity=entity,
            attribute=attribute,
            value=value,
            version=version,
            valid_from=valid_from,
            valid_to=None,
            source_event=source_event,
            observation_id=observation_id,
            state_delta_id=state_delta_id,
            created_by_process_id=created_by_process_id,
            confidence=confidence,
            created_at=now,
        )
        self._insert_history(history)
        self._upsert_current(history, now)

        return StateEntry(
            entity=entity,
            attribute=attribute,
            value=value,
            version=version,
            source_event=source_event,
            observation_id=observation_id,
            state_delta_id=state_delta_id,
            created_by_process_id=created_by_process_id,
            confidence=confidence,
            history_id=history.id,
            updated_at=now,
        )

    def _insert_history(self, h: StateHistoryEntry) -> None:
        self.db.execute(
            """
            INSERT INTO world_state_history
                (id, entity, attribute, value, version, valid_from, valid_to,
                 source_event, observation_id, state_delta_id, created_by_process_id,
                 confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(h.id),
                h.entity,
                h.attribute,
                dumps(h.value),
                h.version,
                h.valid_from.isoformat(),
                h.valid_to.isoformat() if h.valid_to else None,
                str(h.source_event) if h.source_event else None,
                str(h.observation_id) if h.observation_id else None,
                str(h.state_delta_id) if h.state_delta_id else None,
                str(h.created_by_process_id) if h.created_by_process_id else None,
                h.confidence,
                h.created_at.isoformat(),
            ),
        )

    def _upsert_current(self, h: StateHistoryEntry, now: datetime) -> None:
        self.db.execute(
            """
            INSERT INTO world_state_current
                (entity, attribute, value, version, history_id, source_event,
                 observation_id, state_delta_id, created_by_process_id, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity, attribute) DO UPDATE SET
                value = excluded.value,
                version = excluded.version,
                history_id = excluded.history_id,
                source_event = excluded.source_event,
                observation_id = excluded.observation_id,
                state_delta_id = excluded.state_delta_id,
                created_by_process_id = excluded.created_by_process_id,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (
                h.entity,
                h.attribute,
                dumps(h.value),
                h.version,
                str(h.id),
                str(h.source_event) if h.source_event else None,
                str(h.observation_id) if h.observation_id else None,
                str(h.state_delta_id) if h.state_delta_id else None,
                str(h.created_by_process_id) if h.created_by_process_id else None,
                h.confidence,
                now.isoformat(),
            ),
        )

    # --- current reads -----------------------------------------------------

    def get(self, entity: str, attribute: str, default: Any = None) -> Any:
        """Return the current value of a fact, or ``default`` if unset."""
        current = self.get_current(entity, attribute)
        return current.value if current is not None else default

    def get_current(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the current :class:`StateEntry`, or ``None`` if unset."""
        row = self.db.query_one(
            "SELECT * FROM world_state_current WHERE entity = ? AND attribute = ?",
            (entity, attribute),
        )
        return self._row_to_current(row) if row else None

    # ``get_entry`` kept as an alias so existing callers keep working.
    get_entry = get_current

    def all_current(self) -> list[StateEntry]:
        """Return every current fact."""
        rows = self.db.query(
            "SELECT * FROM world_state_current ORDER BY entity ASC, attribute ASC"
        )
        return [self._row_to_current(r) for r in rows]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return all current facts as ``{entity: {attribute: value}}``."""
        snapshot: dict[str, dict[str, Any]] = {}
        for entry in self.all_current():
            snapshot.setdefault(entry.entity, {})[entry.attribute] = entry.value
        return snapshot

    # --- history reads -----------------------------------------------------

    def get_history(self, entity: str, attribute: str) -> list[StateHistoryEntry]:
        """Return every version of a fact, oldest first."""
        rows = self.db.query(
            "SELECT * FROM world_state_history WHERE entity = ? AND attribute = ? "
            "ORDER BY version ASC",
            (entity, attribute),
        )
        return [self._row_to_history(r) for r in rows]

    def get_state_at_version(
        self, entity: str, attribute: str, version: int
    ) -> StateHistoryEntry | None:
        """Return a specific historical version of a fact."""
        row = self.db.query_one(
            "SELECT * FROM world_state_history "
            "WHERE entity = ? AND attribute = ? AND version = ?",
            (entity, attribute, version),
        )
        return self._row_to_history(row) if row else None

    def get_state_at_time(
        self, entity: str, attribute: str, timestamp: datetime
    ) -> StateHistoryEntry | None:
        """Return the version that was current at ``timestamp`` (if any)."""
        ts = timestamp.isoformat()
        row = self.db.query_one(
            "SELECT * FROM world_state_history "
            "WHERE entity = ? AND attribute = ? AND valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY version DESC LIMIT 1",
            (entity, attribute, ts, ts),
        )
        return self._row_to_history(row) if row else None

    def get_history_entry(self, history_id: uuid.UUID) -> StateHistoryEntry | None:
        """Return a history row by id (used for provenance)."""
        row = self.db.query_one(
            "SELECT * FROM world_state_history WHERE id = ?", (str(history_id),)
        )
        return self._row_to_history(row) if row else None

    # --- projection rebuild ------------------------------------------------

    def rebuild_current_state(self) -> int:
        """Rebuild ``world_state_current`` from history; return facts rebuilt.

        History is the source of truth: this recomputes the projection so a
        deleted or corrupted ``world_state_current`` can be fully restored.
        """
        with self.db.atomic():
            self.db.execute("DELETE FROM world_state_current")
            rows = self.db.query(
                """
                SELECT h.* FROM world_state_history h
                JOIN (
                    SELECT entity, attribute, MAX(version) AS max_version
                    FROM world_state_history
                    GROUP BY entity, attribute
                ) m ON h.entity = m.entity
                   AND h.attribute = m.attribute
                   AND h.version = m.max_version
                """
            )
            count = 0
            for row in rows:
                history = self._row_to_history(row)
                self._upsert_current(history, utcnow())
                count += 1
        return count

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _row_to_current(row) -> StateEntry:
        return StateEntry(
            entity=row["entity"],
            attribute=row["attribute"],
            value=loads(row["value"]),
            version=row["version"],
            source_event=_uuid(row["source_event"]),
            observation_id=_uuid(row["observation_id"]),
            state_delta_id=_uuid(row["state_delta_id"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            confidence=row["confidence"],
            history_id=_uuid(row["history_id"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_history(row) -> StateHistoryEntry:
        return StateHistoryEntry(
            entity=row["entity"],
            attribute=row["attribute"],
            value=loads(row["value"]),
            version=row["version"],
            valid_from=datetime.fromisoformat(row["valid_from"]),
            valid_to=datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            source_event=_uuid(row["source_event"]),
            observation_id=_uuid(row["observation_id"]),
            state_delta_id=_uuid(row["state_delta_id"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            confidence=row["confidence"],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
