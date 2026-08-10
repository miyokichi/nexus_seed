"""Persistence for :class:`~nexus_seed.core.continuation.Continuation`."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.continuation import Continuation
from .database import Database, dumps, loads


class ContinuationStore:
    """Stores continuations so suspended processes can be resumed after restart."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, continuation: Continuation) -> Continuation:
        """Insert or update a continuation."""
        self.db.execute(
            """
            INSERT INTO continuations
                (id, process_instance_id, resume_point, waiting_for,
                 saved_process_state, context_ref, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                resume_point = excluded.resume_point,
                waiting_for = excluded.waiting_for,
                saved_process_state = excluded.saved_process_state,
                context_ref = excluded.context_ref
            """,
            (
                str(continuation.id),
                str(continuation.process_instance_id),
                continuation.resume_point,
                dumps(continuation.waiting_for),
                dumps(continuation.saved_process_state),
                continuation.context_ref,
                continuation.created_at.isoformat(),
            ),
        )
        return continuation

    def get(self, continuation_id: uuid.UUID) -> Continuation | None:
        """Return the continuation with ``continuation_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM continuations WHERE id = ?", (str(continuation_id),)
        )
        return self._row_to_continuation(row) if row else None

    def for_instance(self, instance_id: uuid.UUID) -> Continuation | None:
        """Return the (single) continuation for ``instance_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM continuations WHERE process_instance_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(instance_id),),
        )
        return self._row_to_continuation(row) if row else None

    def all(self) -> list[Continuation]:
        """Return all stored continuations in creation order."""
        rows = self.db.query("SELECT * FROM continuations ORDER BY created_at ASC")
        return [self._row_to_continuation(r) for r in rows]

    def delete(self, continuation_id: uuid.UUID) -> None:
        """Delete the continuation with ``continuation_id``."""
        self.db.execute(
            "DELETE FROM continuations WHERE id = ?", (str(continuation_id),)
        )

    @staticmethod
    def _row_to_continuation(row) -> Continuation:
        return Continuation(
            process_instance_id=uuid.UUID(row["process_instance_id"]),
            resume_point=row["resume_point"],
            waiting_for=loads(row["waiting_for"]),
            saved_process_state=loads(row["saved_process_state"]),
            context_ref=row["context_ref"],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
