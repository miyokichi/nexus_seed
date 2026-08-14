"""Persistence for the decision layer: evaluations, proposals, selections, replans.

Append-only by design (Invariant 78).  A decision history that can be
overwritten explains nothing afterwards — the question a reader brings to this
table is usually "why did it choose *that*", and answering it needs the options
that were not taken and the attempts that did not work, not just the current
state.

The two UNIQUE keys are the ones that make the pipeline idempotent under
redelivery: one evaluation per plan, and one replan attempt per
``(need, failed plan)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.event import utcnow
from ..decision.models import (
    PlanEvaluation,
    PlanSelection,
    PlanSelectionProposal,
    ReplanAttempt,
    SelectionMethod,
    SelectionProposalStatus,
)
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _uuids(raw) -> list[uuid.UUID]:
    return [uuid.UUID(v) for v in (loads(raw) or [])]


class DecisionStore:
    """Stores what was evaluated, what was suggested, and what was decided."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- evaluations -------------------------------------------------------

    def save_evaluation(self, evaluation: PlanEvaluation) -> PlanEvaluation:
        """Store one plan's estimates (idempotent per plan)."""
        self.db.execute(
            """
            INSERT INTO plan_evaluations
                (id, plan_id, work_requirement_id, fingerprint,
                 estimated_cost, estimated_latency, estimated_risk,
                 estimated_quality, estimated_reliability,
                 node_count, depth, human_approval_required,
                 reasons_json, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO NOTHING
            """,
            (
                str(evaluation.id),
                str(evaluation.plan_id),
                str(evaluation.work_requirement_id)
                if evaluation.work_requirement_id
                else None,
                evaluation.fingerprint,
                evaluation.estimated_cost,
                evaluation.estimated_latency,
                evaluation.estimated_risk,
                evaluation.estimated_quality,
                evaluation.estimated_reliability,
                evaluation.node_count,
                evaluation.depth,
                int(evaluation.human_approval_required),
                dumps(list(evaluation.reasons)),
                dumps(dict(evaluation.metadata)),
                evaluation.created_at.isoformat(),
            ),
        )
        return evaluation

    def get_evaluation(self, plan_id: uuid.UUID) -> PlanEvaluation | None:
        row = self.db.query_one(
            "SELECT * FROM plan_evaluations WHERE plan_id = ?", (str(plan_id),)
        )
        return self._evaluation(row) if row else None

    def evaluations_for_work(self, work_requirement_id: uuid.UUID) -> list[PlanEvaluation]:
        rows = self.db.query(
            "SELECT * FROM plan_evaluations WHERE work_requirement_id = ? "
            "ORDER BY created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._evaluation(r) for r in rows]

    # --- selection proposals -----------------------------------------------

    def save_proposal(self, proposal: PlanSelectionProposal) -> PlanSelectionProposal:
        self.db.execute(
            """
            INSERT INTO plan_selection_proposals
                (id, work_requirement_id, candidate_plan_ids_json, selected_plan_id,
                 confidence, rationale, status, reasons_json, context_snapshot_id,
                 llm_invocation_id, created_by_process_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                selected_plan_id = excluded.selected_plan_id,
                reasons_json = excluded.reasons_json,
                updated_at = excluded.updated_at
            """,
            (
                str(proposal.id),
                str(proposal.work_requirement_id),
                dumps([str(i) for i in proposal.candidate_plan_ids]),
                str(proposal.selected_plan_id) if proposal.selected_plan_id else None,
                proposal.confidence,
                proposal.rationale,
                proposal.status.value,
                dumps(list(proposal.reasons)),
                str(proposal.context_snapshot_id) if proposal.context_snapshot_id else None,
                str(proposal.llm_invocation_id) if proposal.llm_invocation_id else None,
                str(proposal.created_by_process_id)
                if proposal.created_by_process_id
                else None,
                proposal.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return proposal

    def update_proposal_status(
        self, proposal_id: uuid.UUID, status: SelectionProposalStatus | str, *, reasons=None
    ) -> None:
        """Transition a proposal, optionally recording why."""
        value = status.value if isinstance(status, SelectionProposalStatus) else status
        if reasons is None:
            self.db.execute(
                "UPDATE plan_selection_proposals SET status = ?, updated_at = ? WHERE id = ?",
                (value, utcnow().isoformat(), str(proposal_id)),
            )
            return
        self.db.execute(
            "UPDATE plan_selection_proposals SET status = ?, reasons_json = ?, "
            "updated_at = ? WHERE id = ?",
            (value, dumps(list(reasons)), utcnow().isoformat(), str(proposal_id)),
        )

    def get_proposal(self, proposal_id: uuid.UUID) -> PlanSelectionProposal | None:
        row = self.db.query_one(
            "SELECT * FROM plan_selection_proposals WHERE id = ?", (str(proposal_id),)
        )
        return self._proposal(row) if row else None

    def proposals_for_work(self, work_requirement_id: uuid.UUID) -> list[PlanSelectionProposal]:
        rows = self.db.query(
            "SELECT * FROM plan_selection_proposals WHERE work_requirement_id = ? "
            "ORDER BY created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._proposal(r) for r in rows]

    # --- selections ---------------------------------------------------------

    def save_selection(self, selection: PlanSelection) -> PlanSelection:
        """Record a decision.  Never an update: history is not overwritten."""
        self.db.execute(
            """
            INSERT INTO plan_selections
                (id, work_requirement_id, selected_plan_id, selection_method,
                 deterministic_score, selection_proposal_id, replan_attempt,
                 considered_plan_ids_json, rejected_plan_ids_json,
                 decision_reasons_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                str(selection.id),
                str(selection.work_requirement_id),
                str(selection.selected_plan_id) if selection.selected_plan_id else None,
                selection.selection_method.value,
                selection.deterministic_score,
                str(selection.selection_proposal_id)
                if selection.selection_proposal_id
                else None,
                selection.replan_attempt,
                dumps([str(i) for i in selection.considered_plan_ids]),
                dumps([str(i) for i in selection.rejected_plan_ids]),
                dumps(list(selection.decision_reasons)),
                selection.created_at.isoformat(),
            ),
        )
        return selection

    def selections_for_work(self, work_requirement_id: uuid.UUID) -> list[PlanSelection]:
        rows = self.db.query(
            "SELECT * FROM plan_selections WHERE work_requirement_id = ? "
            "ORDER BY created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._selection(r) for r in rows]

    def latest_selection(self, work_requirement_id: uuid.UUID) -> PlanSelection | None:
        selections = self.selections_for_work(work_requirement_id)
        return selections[-1] if selections else None

    # --- replan attempts ----------------------------------------------------

    def save_replan_attempt(self, attempt: ReplanAttempt) -> bool:
        """Record an attempt; ``False`` if this one had already been recorded.

        The return value *is* the idempotency guard (spec §92): a redelivered
        ``replan_required`` for the same failed plan gets ``False`` and stops,
        rather than composing a second set of candidates for the same failure.
        """
        cur = self.db.execute(
            """
            INSERT OR IGNORE INTO replan_attempts
                (id, work_requirement_id, previous_plan_id, attempt_number,
                 excluded_fingerprints_json, excluded_definitions_json,
                 candidate_plan_ids_json, selected_plan_id, failure_reason,
                 reasons_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(attempt.id),
                str(attempt.work_requirement_id),
                str(attempt.previous_plan_id) if attempt.previous_plan_id else None,
                attempt.attempt_number,
                dumps(list(attempt.excluded_fingerprints)),
                dumps(list(attempt.excluded_definitions)),
                dumps([str(i) for i in attempt.candidate_plan_ids]),
                str(attempt.selected_plan_id) if attempt.selected_plan_id else None,
                attempt.failure_reason,
                dumps(list(attempt.reasons)),
                attempt.created_at.isoformat(),
            ),
        )
        return cur.rowcount > 0

    def replan_attempts_for_work(self, work_requirement_id: uuid.UUID) -> list[ReplanAttempt]:
        rows = self.db.query(
            "SELECT * FROM replan_attempts WHERE work_requirement_id = ? "
            "ORDER BY attempt_number ASC, created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._replan(r) for r in rows]

    def replan_attempt_exists(
        self, work_requirement_id: uuid.UUID, previous_plan_id: uuid.UUID | None
    ) -> bool:
        """Whether this need has already been replanned after that plan."""
        row = self.db.query_one(
            "SELECT 1 FROM replan_attempts WHERE work_requirement_id = ? "
            "AND previous_plan_id IS ?",
            (
                str(work_requirement_id),
                str(previous_plan_id) if previous_plan_id else None,
            ),
        )
        return row is not None

    # --- row mapping --------------------------------------------------------

    @staticmethod
    def _evaluation(row) -> PlanEvaluation:
        return PlanEvaluation(
            plan_id=uuid.UUID(row["plan_id"]),
            work_requirement_id=_uuid(row["work_requirement_id"]),
            fingerprint=row["fingerprint"],
            estimated_cost=row["estimated_cost"],
            estimated_latency=row["estimated_latency"],
            estimated_risk=row["estimated_risk"],
            estimated_quality=row["estimated_quality"],
            estimated_reliability=row["estimated_reliability"],
            node_count=row["node_count"],
            depth=row["depth"],
            human_approval_required=bool(row["human_approval_required"]),
            reasons=loads(row["reasons_json"]) or [],
            metadata=loads(row["metadata_json"]) or {},
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _proposal(row) -> PlanSelectionProposal:
        return PlanSelectionProposal(
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            candidate_plan_ids=_uuids(row["candidate_plan_ids_json"]),
            selected_plan_id=_uuid(row["selected_plan_id"]),
            confidence=row["confidence"],
            rationale=row["rationale"],
            context_snapshot_id=_uuid(row["context_snapshot_id"]),
            llm_invocation_id=_uuid(row["llm_invocation_id"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            status=SelectionProposalStatus(row["status"]),
            reasons=loads(row["reasons_json"]) or [],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _selection(row) -> PlanSelection:
        return PlanSelection(
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            selected_plan_id=_uuid(row["selected_plan_id"]),
            selection_method=SelectionMethod(row["selection_method"]),
            deterministic_score=row["deterministic_score"],
            selection_proposal_id=_uuid(row["selection_proposal_id"]),
            replan_attempt=row["replan_attempt"],
            considered_plan_ids=_uuids(row["considered_plan_ids_json"]),
            rejected_plan_ids=_uuids(row["rejected_plan_ids_json"]),
            decision_reasons=loads(row["decision_reasons_json"]) or [],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _replan(row) -> ReplanAttempt:
        return ReplanAttempt(
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            previous_plan_id=_uuid(row["previous_plan_id"]),
            attempt_number=row["attempt_number"],
            excluded_fingerprints=loads(row["excluded_fingerprints_json"]) or [],
            excluded_definitions=loads(row["excluded_definitions_json"]) or [],
            candidate_plan_ids=_uuids(row["candidate_plan_ids_json"]),
            selected_plan_id=_uuid(row["selected_plan_id"]),
            failure_reason=row["failure_reason"],
            reasons=loads(row["reasons_json"]) or [],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


__all__ = ["DecisionStore"]
