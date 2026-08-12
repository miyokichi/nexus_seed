"""Persistence for :class:`~nexus_seed.backends.base.LLMInvocation` (audit)."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..backends.base import LLMInvocation
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class LLMInvocationStore:
    """Stores a record of each backend call for provenance/audit."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, invocation: LLMInvocation) -> LLMInvocation:
        """Persist an invocation record (insert)."""
        self.db.execute(
            """
            INSERT INTO llm_invocations
                (id, process_instance_id, activation_id, backend, model,
                 request_metadata, response_metadata, context_snapshot_id,
                 success, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(invocation.id),
                str(invocation.process_instance_id),
                invocation.activation_id,
                invocation.backend,
                invocation.model,
                dumps(invocation.request_metadata),
                dumps(invocation.response_metadata),
                str(invocation.context_snapshot_id) if invocation.context_snapshot_id else None,
                int(invocation.success),
                invocation.error,
                invocation.created_at.isoformat(),
            ),
        )
        return invocation

    def get(self, invocation_id: uuid.UUID) -> LLMInvocation | None:
        """Return the invocation with ``invocation_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM llm_invocations WHERE id = ?", (str(invocation_id),)
        )
        return self._row(row) if row else None

    def for_instance(self, instance_id: uuid.UUID) -> list[LLMInvocation]:
        """Return all invocations by an instance, oldest first."""
        rows = self.db.query(
            "SELECT * FROM llm_invocations WHERE process_instance_id = ? "
            "ORDER BY created_at ASC",
            (str(instance_id),),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> LLMInvocation:
        return LLMInvocation(
            process_instance_id=uuid.UUID(row["process_instance_id"]),
            backend=row["backend"],
            activation_id=row["activation_id"],
            model=row["model"],
            request_metadata=loads(row["request_metadata"]) or {},
            response_metadata=loads(row["response_metadata"]) or {},
            context_snapshot_id=_uuid(row["context_snapshot_id"]),
            success=bool(row["success"]),
            error=row["error"],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
