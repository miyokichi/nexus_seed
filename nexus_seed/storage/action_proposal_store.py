"""Persistence for :class:`~nexus_seed.actions.models.ActionProposal`.

Proposals are never deleted, not even after they run (spec §9): the record of
what the system *wanted* to do is as auditable as what it did.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..actions.models import ActionProposal, ActionProposalStatus, RiskLevel
from ..core.event import utcnow
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ActionProposalStore:
    """Stores action proposals and their authorization/outcome status."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, proposal: ActionProposal) -> ActionProposal:
        """Insert or update a proposal (idempotent by id)."""
        self.db.execute(
            """
            INSERT INTO action_proposals
                (id, created_by_process_id, source_work_requirement_id, trigger_event_id,
                 context_snapshot_id, backend, action_type, target, parameters_json,
                 required_permissions_json, declared_side_effects_json, risk_level,
                 status, rationale, idempotency_key, root_proposal_id,
                 replaces_proposal_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                context_snapshot_id = excluded.context_snapshot_id,
                parameters_json = excluded.parameters_json,
                required_permissions_json = excluded.required_permissions_json,
                declared_side_effects_json = excluded.declared_side_effects_json,
                risk_level = excluded.risk_level,
                status = excluded.status,
                rationale = excluded.rationale,
                updated_at = excluded.updated_at
            """,
            (
                str(proposal.id),
                str(proposal.created_by_process_id) if proposal.created_by_process_id else None,
                str(proposal.source_work_requirement_id)
                if proposal.source_work_requirement_id
                else None,
                str(proposal.trigger_event_id) if proposal.trigger_event_id else None,
                str(proposal.context_snapshot_id) if proposal.context_snapshot_id else None,
                proposal.backend,
                proposal.action_type,
                proposal.target,
                dumps(proposal.parameters),
                dumps(list(proposal.required_permissions)),
                dumps(list(proposal.declared_side_effects)),
                proposal.risk_level.value
                if isinstance(proposal.risk_level, RiskLevel)
                else str(proposal.risk_level),
                proposal.status.value,
                proposal.rationale,
                proposal.idempotency_key,
                str(proposal.root_proposal_id) if proposal.root_proposal_id else None,
                str(proposal.replaces_proposal_id) if proposal.replaces_proposal_id else None,
                proposal.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return proposal

    def update_status(
        self, proposal_id: uuid.UUID, status: ActionProposalStatus | str
    ) -> None:
        """Transition a proposal to a new status."""
        value = status.value if isinstance(status, ActionProposalStatus) else status
        self.db.execute(
            "UPDATE action_proposals SET status = ?, updated_at = ? WHERE id = ?",
            (value, utcnow().isoformat(), str(proposal_id)),
        )

    def get(self, proposal_id: uuid.UUID) -> ActionProposal | None:
        """Return the proposal with ``proposal_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM action_proposals WHERE id = ?", (str(proposal_id),)
        )
        return self._row(row) if row else None

    def all(self) -> list[ActionProposal]:
        """Return all proposals in creation order."""
        rows = self.db.query("SELECT * FROM action_proposals ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    def by_status(self, status: ActionProposalStatus | str) -> list[ActionProposal]:
        """Return all proposals currently in ``status``."""
        value = status.value if isinstance(status, ActionProposalStatus) else status
        rows = self.db.query(
            "SELECT * FROM action_proposals WHERE status = ? ORDER BY created_at ASC",
            (value,),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> ActionProposal:
        risk = RiskLevel.coerce(row["risk_level"]) or RiskLevel.CRITICAL
        return ActionProposal(
            backend=row["backend"],
            action_type=row["action_type"],
            target=row["target"],
            parameters=loads(row["parameters_json"]) or {},
            required_permissions=loads(row["required_permissions_json"]) or [],
            declared_side_effects=loads(row["declared_side_effects_json"]) or [],
            risk_level=risk,
            rationale=row["rationale"],
            status=ActionProposalStatus(row["status"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            source_work_requirement_id=_uuid(row["source_work_requirement_id"]),
            trigger_event_id=_uuid(row["trigger_event_id"]),
            context_snapshot_id=_uuid(row["context_snapshot_id"]),
            idempotency_key=row["idempotency_key"],
            root_proposal_id=_uuid(row["root_proposal_id"]),
            replaces_proposal_id=_uuid(row["replaces_proposal_id"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
