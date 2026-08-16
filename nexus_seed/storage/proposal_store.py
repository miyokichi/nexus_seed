"""Persistence for :class:`~nexus_seed.intelligence.proposal.InterpretationProposal`."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.event import utcnow
from ..intelligence.proposal import (
    InterpretationProposal,
    ProposalDecision,
    ProposedStateDelta,
)
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ProposalStore:
    """Stores interpretation proposals and their decisions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, proposal: InterpretationProposal) -> InterpretationProposal:
        """Insert or update a proposal (idempotent by id)."""
        self.db.execute(
            """
            INSERT INTO interpretation_proposals
                (id, source_event_id, created_by_process_id, context_snapshot_id,
                 llm_invocation_id, proposal_json, confidence, decision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                proposal_json = excluded.proposal_json,
                confidence = excluded.confidence,
                decision = excluded.decision,
                context_snapshot_id = excluded.context_snapshot_id,
                llm_invocation_id = excluded.llm_invocation_id,
                updated_at = excluded.updated_at
            """,
            (
                str(proposal.id),
                str(proposal.source_event_id) if proposal.source_event_id else None,
                str(proposal.created_by_process_id) if proposal.created_by_process_id else None,
                str(proposal.context_snapshot_id) if proposal.context_snapshot_id else None,
                str(proposal.llm_invocation_id) if proposal.llm_invocation_id else None,
                dumps(proposal.to_dict()),
                proposal.confidence,
                proposal.decision.value,
                proposal.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return proposal

    def update_decision(self, proposal_id: uuid.UUID, decision: ProposalDecision | str) -> None:
        """Transition a proposal to a new decision."""
        value = decision.value if isinstance(decision, ProposalDecision) else decision
        self.db.execute(
            "UPDATE interpretation_proposals SET decision = ?, updated_at = ? WHERE id = ?",
            (value, utcnow().isoformat(), str(proposal_id)),
        )

    def get(self, proposal_id: uuid.UUID) -> InterpretationProposal | None:
        """Return the proposal with ``proposal_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM interpretation_proposals WHERE id = ?", (str(proposal_id),)
        )
        return self._row(row) if row else None

    def all(self) -> list[InterpretationProposal]:
        """Return all proposals in creation order."""
        rows = self.db.query("SELECT * FROM interpretation_proposals ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> InterpretationProposal:
        body = loads(row["proposal_json"]) or {}
        return InterpretationProposal(
            subject=body.get("subject", ""),
            predicate=body.get("predicate", ""),
            extracted=body.get("extracted", {}),
            proposed_state_deltas=[
                ProposedStateDelta.from_dict(d) for d in body.get("proposed_state_deltas", [])
            ],
            proposed_state_deltas_declared=isinstance(
                body.get("proposed_state_deltas"), list
            ),
            confidence=row["confidence"],
            rationale=body.get("rationale"),
            source_event_id=_uuid(row["source_event_id"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            context_snapshot_id=_uuid(row["context_snapshot_id"]),
            llm_invocation_id=_uuid(row["llm_invocation_id"]),
            decision=ProposalDecision(row["decision"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
