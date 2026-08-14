"""Persistence for the self-extension layer: gaps, proposals, decisions.

Two UNIQUE keys carry the phase's idempotency, and neither is an id:

* ``capability_gaps (work_requirement_id, missing_key)`` — a redelivered
  ``capability_missing`` finds the gap it already opened instead of opening a
  second one (spec §11, §127).
* ``extension_proposals (fingerprint)`` — a re-run of the same analysis over the
  same registry produces the same proposal content, and therefore the same row
  (spec §67–§68).

Decisions are append-only (Invariant 92): a proposal reviewed twice keeps both
records.  Nothing here is ever deleted — the record of what the system wanted to
become is as auditable as what it did.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from ..capabilities.models import CapabilityRequirement
from ..core.event import utcnow
from ..extension.models import (
    AcquisitionFeasibility,
    CapabilityGap,
    CapabilityGapStatus,
    ExtensionDecision,
    ExtensionDecisionRecord,
    ExtensionProposal,
    ExtensionProposalStatus,
    ExtensionRisk,
    ExtensionStrategy,
    ProposedComponent,
)
from .database import Database, dumps, loads

logger = logging.getLogger("nexus_seed.storage.extension")


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ExtensionStore:
    """Stores what the system lacks, what it proposed about it, and what was decided."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- capability gaps ----------------------------------------------------

    def save_gap(self, gap: CapabilityGap) -> CapabilityGap:
        """Persist a gap, collapsing a redelivery onto the existing row.

        ``ON CONFLICT DO NOTHING`` on the logical key rather than on the id:
        two activations deriving the same gap from the same event produce
        different ids for the same deficiency, and the second must not create a
        second gap (spec §11).
        """
        self.db.execute(
            """
            INSERT INTO capability_gaps
                (id, work_requirement_id, missing_key, required_capabilities_json,
                 missing_capabilities_json, current_partial_providers_json,
                 reason, source_match_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_requirement_id, missing_key) DO NOTHING
            """,
            (
                str(gap.id),
                str(gap.work_requirement_id),
                gap.missing_key,
                dumps([r.to_dict() for r in gap.required_capabilities]),
                dumps([r.to_dict() for r in gap.missing_capabilities]),
                dumps(list(gap.current_partial_providers)),
                gap.reason,
                str(gap.source_match_id) if gap.source_match_id else None,
                gap.status.value,
                gap.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return gap

    def update_gap_status(
        self, gap_id: uuid.UUID, status: CapabilityGapStatus | str
    ) -> None:
        """Transition a gap."""
        value = status.value if isinstance(status, CapabilityGapStatus) else status
        self.db.execute(
            "UPDATE capability_gaps SET status = ?, updated_at = ? WHERE id = ?",
            (value, utcnow().isoformat(), str(gap_id)),
        )

    def get_gap(self, gap_id: uuid.UUID) -> CapabilityGap | None:
        row = self.db.query_one(
            "SELECT * FROM capability_gaps WHERE id = ?", (str(gap_id),)
        )
        return self._gap(row) if row else None

    def find_gap(
        self, work_requirement_id: uuid.UUID, missing_key: str
    ) -> CapabilityGap | None:
        """The gap for this need and this exact set of missing capabilities."""
        row = self.db.query_one(
            "SELECT * FROM capability_gaps WHERE work_requirement_id = ? "
            "AND missing_key = ?",
            (str(work_requirement_id), missing_key),
        )
        return self._gap(row) if row else None

    def gaps_for_work(self, work_requirement_id: uuid.UUID) -> list[CapabilityGap]:
        """Every gap ever opened for a need, oldest first."""
        rows = self.db.query(
            "SELECT * FROM capability_gaps WHERE work_requirement_id = ? "
            "ORDER BY created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._gap(r) for r in rows]

    def all_gaps(self) -> list[CapabilityGap]:
        rows = self.db.query("SELECT * FROM capability_gaps ORDER BY created_at ASC")
        return [self._gap(r) for r in rows]

    def gaps_by_status(self, status: CapabilityGapStatus | str) -> list[CapabilityGap]:
        value = status.value if isinstance(status, CapabilityGapStatus) else status
        rows = self.db.query(
            "SELECT * FROM capability_gaps WHERE status = ? ORDER BY created_at ASC",
            (value,),
        )
        return [self._gap(r) for r in rows]

    def open_gaps(self) -> list[CapabilityGap]:
        """Every gap that is neither resolved nor cancelled."""
        rows = self.db.query(
            "SELECT * FROM capability_gaps WHERE status NOT IN (?, ?) "
            "ORDER BY created_at ASC",
            (CapabilityGapStatus.RESOLVED.value, CapabilityGapStatus.CANCELLED.value),
        )
        return [self._gap(r) for r in rows]

    # --- extension proposals -------------------------------------------------

    def save_proposal(self, proposal: ExtensionProposal) -> ExtensionProposal:
        """Persist a proposal, unless its content already exists under another id.

        The fingerprint check is done here rather than left to the UNIQUE index
        because this runs inside an activation's transaction: an
        ``IntegrityError`` would roll back the whole activation, whereas
        "somebody already proposed exactly this" is a normal outcome of a
        redelivered event (spec §67).
        """
        existing = self.get_proposal_by_fingerprint(proposal.fingerprint)
        if existing is not None and existing.id != proposal.id:
            logger.info(
                "extension proposal %s duplicates %s (fingerprint %s); keeping the first",
                proposal.id,
                existing.id,
                proposal.fingerprint,
            )
            return existing
        self.db.execute(
            """
            INSERT INTO extension_proposals
                (id, capability_gap_id, work_requirement_id, fingerprint, strategy,
                 declared_strategy, title, description, target_capabilities_json,
                 proposed_components_json, reusable_components_json,
                 required_permissions_json, candidate_strategies_json, analysis_json,
                 estimated_risk, estimated_cost, feasibility, human_approval_required,
                 rationale, source, status, reasons_json, context_snapshot_id,
                 llm_invocation_id, created_by_process_id, root_proposal_id,
                 replaces_proposal_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                reasons_json = excluded.reasons_json,
                estimated_risk = excluded.estimated_risk,
                llm_invocation_id = excluded.llm_invocation_id,
                context_snapshot_id = excluded.context_snapshot_id,
                updated_at = excluded.updated_at
            """,
            (
                str(proposal.id),
                str(proposal.capability_gap_id),
                str(proposal.work_requirement_id) if proposal.work_requirement_id else None,
                proposal.fingerprint,
                proposal.strategy.value if proposal.strategy else None,
                proposal.declared_strategy,
                proposal.title,
                proposal.description,
                dumps([r.to_dict() for r in proposal.target_capabilities]),
                dumps([c.to_dict() for c in proposal.proposed_components]),
                dumps(list(proposal.reusable_components)),
                dumps(list(proposal.required_permissions)),
                dumps(list(proposal.candidate_strategies)),
                dumps(dict(proposal.analysis)),
                proposal.estimated_risk.value,
                proposal.estimated_cost,
                proposal.feasibility.value,
                int(proposal.human_approval_required),
                proposal.rationale,
                proposal.source,
                proposal.status.value,
                dumps(list(proposal.reasons)),
                str(proposal.context_snapshot_id) if proposal.context_snapshot_id else None,
                str(proposal.llm_invocation_id) if proposal.llm_invocation_id else None,
                str(proposal.created_by_process_id)
                if proposal.created_by_process_id
                else None,
                str(proposal.root_proposal_id) if proposal.root_proposal_id else None,
                str(proposal.replaces_proposal_id)
                if proposal.replaces_proposal_id
                else None,
                proposal.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return proposal

    def update_proposal_status(
        self,
        proposal_id: uuid.UUID,
        status: ExtensionProposalStatus | str,
        *,
        reasons=None,
    ) -> None:
        """Transition a proposal, optionally recording why."""
        value = status.value if isinstance(status, ExtensionProposalStatus) else status
        if reasons is None:
            self.db.execute(
                "UPDATE extension_proposals SET status = ?, updated_at = ? WHERE id = ?",
                (value, utcnow().isoformat(), str(proposal_id)),
            )
            return
        self.db.execute(
            "UPDATE extension_proposals SET status = ?, reasons_json = ?, "
            "updated_at = ? WHERE id = ?",
            (value, dumps(list(reasons)), utcnow().isoformat(), str(proposal_id)),
        )

    def get_proposal(self, proposal_id: uuid.UUID) -> ExtensionProposal | None:
        row = self.db.query_one(
            "SELECT * FROM extension_proposals WHERE id = ?", (str(proposal_id),)
        )
        return self._proposal(row) if row else None

    def get_proposal_by_fingerprint(self, fingerprint: str) -> ExtensionProposal | None:
        row = self.db.query_one(
            "SELECT * FROM extension_proposals WHERE fingerprint = ?", (fingerprint,)
        )
        return self._proposal(row) if row else None

    def proposals_for_gap(self, gap_id: uuid.UUID) -> list[ExtensionProposal]:
        """Every proposal made about a gap, oldest first (never overwritten)."""
        rows = self.db.query(
            "SELECT * FROM extension_proposals WHERE capability_gap_id = ? "
            "ORDER BY created_at ASC",
            (str(gap_id),),
        )
        return [self._proposal(r) for r in rows]

    def proposals_in_chain(self, root_proposal_id: uuid.UUID) -> list[ExtensionProposal]:
        """A modify-chain, oldest first (spec §47)."""
        rows = self.db.query(
            "SELECT * FROM extension_proposals WHERE root_proposal_id = ? "
            "ORDER BY created_at ASC",
            (str(root_proposal_id),),
        )
        return [self._proposal(r) for r in rows]

    def all_proposals(self) -> list[ExtensionProposal]:
        rows = self.db.query("SELECT * FROM extension_proposals ORDER BY created_at ASC")
        return [self._proposal(r) for r in rows]

    def proposals_by_status(
        self, status: ExtensionProposalStatus | str
    ) -> list[ExtensionProposal]:
        value = status.value if isinstance(status, ExtensionProposalStatus) else status
        rows = self.db.query(
            "SELECT * FROM extension_proposals WHERE status = ? ORDER BY created_at ASC",
            (value,),
        )
        return [self._proposal(r) for r in rows]

    def live_proposals_for_gap(self, gap_id: uuid.UUID) -> list[ExtensionProposal]:
        """Proposals for a gap that are still on their way to a decision."""
        return [p for p in self.proposals_for_gap(gap_id) if p.status.live]

    # --- decisions ------------------------------------------------------------

    def save_decision(self, record: ExtensionDecisionRecord) -> ExtensionDecisionRecord:
        """Append one decision.  Never an update (Invariant 92)."""
        self.db.execute(
            """
            INSERT INTO extension_decisions
                (id, extension_proposal_id, capability_gap_id, decision, estimated_risk,
                 decided_by_process_id, validation_ok, reasons_json, policy_json,
                 required_permissions_json, granted_permissions_json,
                 reviewed_by_event_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                str(record.id),
                str(record.extension_proposal_id),
                str(record.capability_gap_id) if record.capability_gap_id else None,
                record.decision.value,
                record.estimated_risk.value,
                str(record.decided_by_process_id) if record.decided_by_process_id else None,
                int(record.validation_ok),
                dumps(list(record.reasons)),
                dumps(dict(record.policy)),
                dumps(list(record.required_permissions)),
                dumps(list(record.granted_permissions)),
                str(record.reviewed_by_event_id) if record.reviewed_by_event_id else None,
                record.created_at.isoformat(),
            ),
        )
        return record

    def decisions_for_proposal(
        self, proposal_id: uuid.UUID
    ) -> list[ExtensionDecisionRecord]:
        rows = self.db.query(
            "SELECT * FROM extension_decisions WHERE extension_proposal_id = ? "
            "ORDER BY created_at ASC",
            (str(proposal_id),),
        )
        return [self._decision(r) for r in rows]

    def all_decisions(self) -> list[ExtensionDecisionRecord]:
        rows = self.db.query("SELECT * FROM extension_decisions ORDER BY created_at ASC")
        return [self._decision(r) for r in rows]

    # --- row mapping ----------------------------------------------------------

    @staticmethod
    def _gap(row) -> CapabilityGap:
        return CapabilityGap(
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            required_capabilities=[
                CapabilityRequirement.from_dict(d)
                for d in (loads(row["required_capabilities_json"]) or [])
            ],
            missing_capabilities=[
                CapabilityRequirement.from_dict(d)
                for d in (loads(row["missing_capabilities_json"]) or [])
            ],
            current_partial_providers=loads(row["current_partial_providers_json"]) or [],
            reason=row["reason"],
            source_match_id=_uuid(row["source_match_id"]),
            status=CapabilityGapStatus(row["status"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _proposal(row) -> ExtensionProposal:
        components = [
            ProposedComponent(
                component_type=d.get("component_type", ""),
                name=d.get("name", ""),
                purpose=d.get("purpose", ""),
                provides_capabilities=list(d.get("provides_capabilities") or []),
                requires_capabilities=list(d.get("requires_capabilities") or []),
                required_permissions=list(d.get("required_permissions") or []),
                metadata=d.get("metadata") or {},
            )
            for d in (loads(row["proposed_components_json"]) or [])
        ]
        risk = ExtensionRisk.coerce(row["estimated_risk"]) or ExtensionRisk.CRITICAL
        feasibility = row["feasibility"]
        return ExtensionProposal(
            capability_gap_id=uuid.UUID(row["capability_gap_id"]),
            work_requirement_id=_uuid(row["work_requirement_id"]),
            target_capabilities=[
                CapabilityRequirement.from_dict(d)
                for d in (loads(row["target_capabilities_json"]) or [])
            ],
            strategy=ExtensionStrategy.coerce(row["strategy"]),
            declared_strategy=row["declared_strategy"],
            title=row["title"],
            description=row["description"],
            proposed_components=components,
            reusable_components=loads(row["reusable_components_json"]) or [],
            required_permissions=loads(row["required_permissions_json"]) or [],
            candidate_strategies=loads(row["candidate_strategies_json"]) or [],
            analysis=loads(row["analysis_json"]) or {},
            estimated_risk=risk,
            estimated_cost=row["estimated_cost"],
            feasibility=AcquisitionFeasibility(feasibility)
            if feasibility in AcquisitionFeasibility._value2member_map_
            else AcquisitionFeasibility.UNKNOWN,
            human_approval_required=bool(row["human_approval_required"]),
            rationale=row["rationale"],
            source=row["source"],
            status=ExtensionProposalStatus(row["status"]),
            reasons=loads(row["reasons_json"]) or [],
            context_snapshot_id=_uuid(row["context_snapshot_id"]),
            llm_invocation_id=_uuid(row["llm_invocation_id"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            root_proposal_id=_uuid(row["root_proposal_id"]),
            replaces_proposal_id=_uuid(row["replaces_proposal_id"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _decision(row) -> ExtensionDecisionRecord:
        return ExtensionDecisionRecord(
            extension_proposal_id=uuid.UUID(row["extension_proposal_id"]),
            capability_gap_id=_uuid(row["capability_gap_id"]),
            decision=ExtensionDecision(row["decision"]),
            estimated_risk=ExtensionRisk.coerce(row["estimated_risk"])
            or ExtensionRisk.CRITICAL,
            decided_by_process_id=_uuid(row["decided_by_process_id"]),
            validation_ok=bool(row["validation_ok"]),
            reasons=loads(row["reasons_json"]) or [],
            policy=loads(row["policy_json"]) or {},
            required_permissions=loads(row["required_permissions_json"]) or [],
            granted_permissions=loads(row["granted_permissions_json"]) or [],
            reviewed_by_event_id=_uuid(row["reviewed_by_event_id"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


__all__ = ["ExtensionStore"]
