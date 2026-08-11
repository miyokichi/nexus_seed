"""Persistence for :class:`~nexus_seed.context.models.ContextSnapshot`.

Snapshots are an audit trail of what each activation compiled — never the
source of truth, and never used to resume a process.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..context.models import ContextSnapshot
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ContextSnapshotStore:
    """Stores per-activation context snapshots for debug/audit."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        """Persist a context snapshot (insert)."""
        self.db.execute(
            """
            INSERT INTO context_snapshots
                (id, process_instance_id, activation_id, trigger_event_id,
                 context_json, compiled_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(snapshot.id),
                str(snapshot.process_instance_id),
                snapshot.activation_id,
                str(snapshot.trigger_event_id) if snapshot.trigger_event_id else None,
                dumps(snapshot.context_json),
                snapshot.compiled_at.isoformat(),
            ),
        )
        return snapshot

    def get(self, snapshot_id: uuid.UUID) -> ContextSnapshot | None:
        """Return the snapshot with ``snapshot_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM context_snapshots WHERE id = ?", (str(snapshot_id),)
        )
        return self._row(row) if row else None

    def for_instance(self, instance_id: uuid.UUID) -> list[ContextSnapshot]:
        """Return all snapshots for an instance, oldest first."""
        rows = self.db.query(
            "SELECT * FROM context_snapshots WHERE process_instance_id = ? "
            "ORDER BY compiled_at ASC",
            (str(instance_id),),
        )
        return [self._row(r) for r in rows]

    def latest_for_instance(self, instance_id: uuid.UUID) -> ContextSnapshot | None:
        """Return the most recent snapshot for an instance, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM context_snapshots WHERE process_instance_id = ? "
            "ORDER BY compiled_at DESC LIMIT 1",
            (str(instance_id),),
        )
        return self._row(row) if row else None

    @staticmethod
    def _row(row) -> ContextSnapshot:
        return ContextSnapshot(
            process_instance_id=uuid.UUID(row["process_instance_id"]),
            context_json=loads(row["context_json"]) or {},
            trigger_event_id=_uuid(row["trigger_event_id"]),
            activation_id=row["activation_id"],
            id=uuid.UUID(row["id"]),
            compiled_at=datetime.fromisoformat(row["compiled_at"]),
        )
