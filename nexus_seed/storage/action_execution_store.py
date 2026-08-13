"""Persistence for :class:`~nexus_seed.actions.models.ActionExecution`.

This table is the *journal of external effects*.  Failed attempts are written
here exactly like successful ones (Invariant 26), and a SUCCEEDED row is what
the idempotency guard reads to decide an effect has already happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..actions.models import ActionExecution, ActionExecutionStatus
from .database import Database, dumps, loads


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class ActionExecutionStore:
    """Stores every attempt to perform an approved action."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, execution: ActionExecution) -> ActionExecution:
        """Insert or update an execution record (idempotent by id)."""
        self.db.execute(
            """
            INSERT INTO action_executions
                (id, action_proposal_id, process_instance_id, backend, action_type,
                 status, attempt, idempotency_key, result_json, error,
                 started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                result_json = excluded.result_json,
                error = excluded.error,
                completed_at = excluded.completed_at
            """,
            (
                str(execution.id),
                str(execution.action_proposal_id),
                str(execution.process_instance_id),
                execution.backend,
                execution.action_type,
                execution.status.value,
                execution.attempt,
                execution.idempotency_key,
                dumps(execution.result) if execution.result is not None else None,
                execution.error,
                execution.started_at.isoformat(),
                execution.completed_at.isoformat() if execution.completed_at else None,
            ),
        )
        return execution

    def get(self, execution_id: uuid.UUID) -> ActionExecution | None:
        """Return the execution with ``execution_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM action_executions WHERE id = ?", (str(execution_id),)
        )
        return self._row(row) if row else None

    def for_proposal(self, proposal_id: uuid.UUID) -> list[ActionExecution]:
        """Return every attempt made for ``proposal_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM action_executions WHERE action_proposal_id = ? "
            "ORDER BY attempt ASC, started_at ASC",
            (str(proposal_id),),
        )
        return [self._row(r) for r in rows]

    def succeeded_for_key(self, idempotency_key: str | None) -> ActionExecution | None:
        """Return a SUCCEEDED execution sharing ``idempotency_key``, if any.

        This is the durable half of the idempotency guard: if it returns a row,
        the external effect is known to have happened and must not be repeated.
        """
        if not idempotency_key:
            return None
        row = self.db.query_one(
            "SELECT * FROM action_executions WHERE idempotency_key = ? AND status = ? "
            "ORDER BY started_at ASC LIMIT 1",
            (idempotency_key, ActionExecutionStatus.SUCCEEDED.value),
        )
        return self._row(row) if row else None

    def all(self) -> list[ActionExecution]:
        """Return all executions, oldest first."""
        rows = self.db.query("SELECT * FROM action_executions ORDER BY started_at ASC")
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> ActionExecution:
        return ActionExecution(
            action_proposal_id=uuid.UUID(row["action_proposal_id"]),
            process_instance_id=uuid.UUID(row["process_instance_id"]),
            backend=row["backend"],
            action_type=row["action_type"],
            status=ActionExecutionStatus(row["status"]),
            attempt=row["attempt"],
            idempotency_key=row["idempotency_key"],
            result=loads(row["result_json"]),
            error=row["error"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=_dt(row["completed_at"]),
            id=uuid.UUID(row["id"]),
        )
