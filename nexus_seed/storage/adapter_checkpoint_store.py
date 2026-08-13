"""Persistence for :class:`~nexus_seed.ingress.models.AdapterCheckpoint`.

Kept in its own table, separate from ``continuations``, because the two answer
different questions and must not be confused (Invariant 33):

    continuations       where *our* processes resume
    adapter_checkpoints how much of the *world* we have already looked at
"""

from __future__ import annotations

from datetime import datetime

from ..core.event import utcnow
from ..ingress.models import AdapterCheckpoint
from .database import Database, dumps, loads


class AdapterCheckpointStore:
    """Stores the observation position of pull/watch adapters."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, checkpoint: AdapterCheckpoint) -> AdapterCheckpoint:
        """Insert or advance a checkpoint (idempotent by adapter + stream)."""
        checkpoint.updated_at = utcnow()
        self.db.execute(
            """
            INSERT INTO adapter_checkpoints
                (adapter_id, stream_key, cursor, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(adapter_id, stream_key) DO UPDATE SET
                cursor = excluded.cursor,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                checkpoint.adapter_id,
                checkpoint.stream_key,
                checkpoint.cursor,
                dumps(checkpoint.metadata),
                checkpoint.updated_at.isoformat(),
            ),
        )
        return checkpoint

    def get(self, adapter_id: str, stream_key: str) -> AdapterCheckpoint | None:
        """Return one checkpoint, or ``None`` if the stream is unobserved."""
        row = self.db.query_one(
            "SELECT * FROM adapter_checkpoints WHERE adapter_id = ? AND stream_key = ?",
            (adapter_id, stream_key),
        )
        return self._row(row) if row else None

    def for_adapter(self, adapter_id: str) -> list[AdapterCheckpoint]:
        """Return every stream checkpoint held by ``adapter_id``."""
        rows = self.db.query(
            "SELECT * FROM adapter_checkpoints WHERE adapter_id = ? ORDER BY stream_key ASC",
            (adapter_id,),
        )
        return [self._row(r) for r in rows]

    def cursors_for_adapter(self, adapter_id: str) -> dict[str, str | None]:
        """Return ``{stream_key: cursor}`` for ``adapter_id`` (one query)."""
        return {c.stream_key: c.cursor for c in self.for_adapter(adapter_id)}

    def delete(self, adapter_id: str, stream_key: str) -> None:
        """Forget a stream (e.g. a watched file that no longer exists)."""
        self.db.execute(
            "DELETE FROM adapter_checkpoints WHERE adapter_id = ? AND stream_key = ?",
            (adapter_id, stream_key),
        )

    def all(self) -> list[AdapterCheckpoint]:
        """Return every checkpoint."""
        rows = self.db.query(
            "SELECT * FROM adapter_checkpoints ORDER BY adapter_id ASC, stream_key ASC"
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> AdapterCheckpoint:
        return AdapterCheckpoint(
            adapter_id=row["adapter_id"],
            stream_key=row["stream_key"],
            cursor=row["cursor"],
            metadata=loads(row["metadata_json"]) or {},
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
