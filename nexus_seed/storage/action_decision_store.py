"""Persistence for :class:`~nexus_seed.actions.models.ActionDecisionRecord`.

Answers "on whose authority did this run?" — which ProcessDefinition's grants
were consulted, what the proposal declared, what the backend mandated, and
which human review event (if any) settled it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..actions.models import ActionDecision, ActionDecisionRecord, RiskLevel
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ActionDecisionStore:
    """Stores authorization decisions with their permission provenance."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, record: ActionDecisionRecord) -> ActionDecisionRecord:
        """Persist a decision record (insert)."""
        self.db.execute(
            """
            INSERT OR REPLACE INTO action_decisions
                (id, action_proposal_id, decision, risk_level, decided_by_process_id,
                 process_definition_name, process_definition_version,
                 granted_permissions_json, required_permissions_json,
                 mandatory_permissions_json, reasons_json, policy_json,
                 reviewed_by_event_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.id),
                str(record.action_proposal_id),
                record.decision.value,
                record.risk_level.value,
                str(record.decided_by_process_id) if record.decided_by_process_id else None,
                record.process_definition_name,
                record.process_definition_version,
                dumps(list(record.granted_permissions)),
                dumps(list(record.required_permissions)),
                dumps(list(record.mandatory_permissions)),
                dumps(list(record.reasons)),
                dumps(record.policy),
                str(record.reviewed_by_event_id) if record.reviewed_by_event_id else None,
                record.created_at.isoformat(),
            ),
        )
        return record

    def get(self, record_id: uuid.UUID) -> ActionDecisionRecord | None:
        """Return the decision with ``record_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM action_decisions WHERE id = ?", (str(record_id),)
        )
        return self._row(row) if row else None

    def for_proposal(self, proposal_id: uuid.UUID) -> list[ActionDecisionRecord]:
        """Return every decision made about ``proposal_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM action_decisions WHERE action_proposal_id = ? "
            "ORDER BY created_at ASC",
            (str(proposal_id),),
        )
        return [self._row(r) for r in rows]

    def all(self) -> list[ActionDecisionRecord]:
        """Return all decisions, oldest first."""
        rows = self.db.query("SELECT * FROM action_decisions ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> ActionDecisionRecord:
        return ActionDecisionRecord(
            action_proposal_id=uuid.UUID(row["action_proposal_id"]),
            decision=ActionDecision(row["decision"]),
            risk_level=RiskLevel.coerce(row["risk_level"]) or RiskLevel.CRITICAL,
            decided_by_process_id=_uuid(row["decided_by_process_id"]),
            process_definition_name=row["process_definition_name"],
            process_definition_version=row["process_definition_version"],
            granted_permissions=loads(row["granted_permissions_json"]) or [],
            required_permissions=loads(row["required_permissions_json"]) or [],
            mandatory_permissions=loads(row["mandatory_permissions_json"]) or [],
            reasons=loads(row["reasons_json"]) or [],
            policy=loads(row["policy_json"]) or {},
            reviewed_by_event_id=_uuid(row["reviewed_by_event_id"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
