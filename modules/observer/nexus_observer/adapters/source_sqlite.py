"""SQLite persistence for explicitly authorized ObservationSources."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..sources import ObservationSource
from nexus_observer._support.storage.database import Database, dumps, loads


class ObservationSourceStore:
    """Persist source configuration and its last polling outcome."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, source: ObservationSource) -> ObservationSource:
        """Insert or update one source."""
        self.db.execute(
            """
            INSERT INTO observation_sources
                (id, name, kind, fields_json, poll_interval_seconds, enabled,
                 config_json, last_checked_at, last_changed_at, last_event_id,
                 last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                fields_json = excluded.fields_json,
                poll_interval_seconds = excluded.poll_interval_seconds,
                enabled = excluded.enabled,
                config_json = excluded.config_json,
                last_checked_at = excluded.last_checked_at,
                last_changed_at = excluded.last_changed_at,
                last_event_id = excluded.last_event_id,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                source.id,
                source.name,
                source.kind,
                dumps(list(source.fields)),
                source.poll_interval_seconds,
                int(source.enabled),
                dumps(source.config),
                source.last_checked_at.isoformat() if source.last_checked_at else None,
                source.last_changed_at.isoformat() if source.last_changed_at else None,
                str(source.last_event_id) if source.last_event_id else None,
                source.last_error,
                source.created_at.isoformat(),
                source.updated_at.isoformat(),
            ),
        )
        return source

    def get(self, source_id: str) -> ObservationSource | None:
        """Return one source, if configured."""
        row = self.db.query_one("SELECT * FROM observation_sources WHERE id = ?", (source_id,))
        return self._row(row) if row else None

    def all(self) -> list[ObservationSource]:
        """Return all sources in creation order."""
        return [
            self._row(row)
            for row in self.db.query("SELECT * FROM observation_sources ORDER BY created_at ASC")
        ]

    @staticmethod
    def _row(row) -> ObservationSource:
        return ObservationSource(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            fields=tuple(loads(row["fields_json"]) or ()),
            poll_interval_seconds=float(row["poll_interval_seconds"]),
            enabled=bool(row["enabled"]),
            config=loads(row["config_json"]) or {},
            last_checked_at=(
                datetime.fromisoformat(row["last_checked_at"])
                if row["last_checked_at"] else None
            ),
            last_changed_at=(
                datetime.fromisoformat(row["last_changed_at"])
                if row["last_changed_at"] else None
            ),
            last_event_id=uuid.UUID(row["last_event_id"]) if row["last_event_id"] else None,
            last_error=row["last_error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


__all__ = ["ObservationSourceStore"]
