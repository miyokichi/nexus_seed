"""SQLite persistence for Phase 5D acquisition coordination and audit."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..autonomy.models import (
    AcquisitionAttempt, AcquisitionStage, AcquisitionStatus, AcquisitionSubscriber,
    AutonomyBudget, AutonomyDecision, AutonomyDecisionKind,
    CapabilityAcquisitionSession,
)
from ..core.event import utcnow
from .database import Database, dumps, loads


def _uuid(value):
    return uuid.UUID(value) if value else None


def _dt(value):
    return datetime.fromisoformat(value) if value else None


class AutonomyStore:
    """Persist sessions, Work subscribers, policy decisions, and attempts."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_session(self, session: CapabilityAcquisitionSession) -> CapabilityAcquisitionSession:
        existing = self.find_by_key(session.acquisition_key)
        if existing is not None and existing.id != session.id:
            return existing
        self.db.execute(
            """INSERT INTO capability_acquisition_sessions
               (id, capability_gap_id, source_work_requirement_id, acquisition_key,
                target_capabilities_json, status, current_stage,
                extension_proposal_id, construction_plan_id, construction_result_id,
                installation_plan_id, parent_session_id, extension_depth,
                construction_attempts, installation_attempts, autonomy_budget_json,
                blocked_reason, review_summary_json, created_at, updated_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status, current_stage=excluded.current_stage,
                 extension_proposal_id=COALESCE(excluded.extension_proposal_id, extension_proposal_id),
                 construction_plan_id=COALESCE(excluded.construction_plan_id, construction_plan_id),
                 construction_result_id=COALESCE(excluded.construction_result_id, construction_result_id),
                 installation_plan_id=COALESCE(excluded.installation_plan_id, installation_plan_id),
                 construction_attempts=excluded.construction_attempts,
                 installation_attempts=excluded.installation_attempts,
                 blocked_reason=excluded.blocked_reason,
                 review_summary_json=excluded.review_summary_json,
                 updated_at=excluded.updated_at, completed_at=excluded.completed_at""",
            (
                str(session.id), str(session.capability_gap_id),
                str(session.source_work_requirement_id), session.acquisition_key,
                dumps(session.target_capabilities), session.status.value,
                session.current_stage.value,
                str(session.extension_proposal_id) if session.extension_proposal_id else None,
                str(session.construction_plan_id) if session.construction_plan_id else None,
                str(session.construction_result_id) if session.construction_result_id else None,
                str(session.installation_plan_id) if session.installation_plan_id else None,
                str(session.parent_session_id) if session.parent_session_id else None,
                session.extension_depth, session.construction_attempts,
                session.installation_attempts, dumps(session.autonomy_budget.to_dict()),
                session.blocked_reason, dumps(session.review_summary),
                session.created_at.isoformat(), utcnow().isoformat(),
                session.completed_at.isoformat() if session.completed_at else None,
            ),
        )
        return session

    def update_session(
        self, session_id, *, status=None, stage=None, extension_proposal_id=None,
        construction_plan_id=None, construction_result_id=None,
        installation_plan_id=None, construction_attempts=None,
        installation_attempts=None, blocked_reason=None, review_summary=None,
        completed_at=None,
    ) -> None:
        fields = ["updated_at=?"]
        values: list = [utcnow().isoformat()]
        mapping = {
            "status": getattr(status, "value", status),
            "current_stage": getattr(stage, "value", stage),
            "extension_proposal_id": str(extension_proposal_id) if extension_proposal_id else None,
            "construction_plan_id": str(construction_plan_id) if construction_plan_id else None,
            "construction_result_id": str(construction_result_id) if construction_result_id else None,
            "installation_plan_id": str(installation_plan_id) if installation_plan_id else None,
            "construction_attempts": construction_attempts,
            "installation_attempts": installation_attempts,
            "blocked_reason": blocked_reason,
            "review_summary_json": dumps(review_summary) if review_summary is not None else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
        }
        # None means "not supplied" for pointer/counter fields.  blocked_reason
        # is cleared by passing an empty string, which is normalized on read.
        for column, value in mapping.items():
            if value is not None:
                fields.append(f"{column}=?")
                values.append(value)
        values.append(str(session_id))
        self.db.execute(
            f"UPDATE capability_acquisition_sessions SET {', '.join(fields)} WHERE id=?",
            tuple(values),
        )

    def get_session(self, session_id) -> CapabilityAcquisitionSession | None:
        row = self.db.query_one(
            "SELECT * FROM capability_acquisition_sessions WHERE id=?", (str(session_id),)
        )
        return self._session(row) if row else None

    def find_by_key(self, acquisition_key: str) -> CapabilityAcquisitionSession | None:
        row = self.db.query_one(
            "SELECT * FROM capability_acquisition_sessions WHERE acquisition_key=?",
            (acquisition_key,),
        )
        return self._session(row) if row else None

    def find_by_proposal(self, proposal_id) -> CapabilityAcquisitionSession | None:
        row = self.db.query_one(
            "SELECT * FROM capability_acquisition_sessions WHERE extension_proposal_id=?",
            (str(proposal_id),),
        )
        return self._session(row) if row else None

    def find_by_construction_plan(self, plan_id) -> CapabilityAcquisitionSession | None:
        row = self.db.query_one(
            "SELECT * FROM capability_acquisition_sessions WHERE construction_plan_id=?",
            (str(plan_id),),
        )
        return self._session(row) if row else None

    def find_by_installation_plan(self, plan_id) -> CapabilityAcquisitionSession | None:
        row = self.db.query_one(
            "SELECT * FROM capability_acquisition_sessions WHERE installation_plan_id=?",
            (str(plan_id),),
        )
        return self._session(row) if row else None

    def sessions(self, *, status=None) -> list[CapabilityAcquisitionSession]:
        if status is None:
            rows = self.db.query("SELECT * FROM capability_acquisition_sessions ORDER BY created_at")
        else:
            rows = self.db.query(
                "SELECT * FROM capability_acquisition_sessions WHERE status=? ORDER BY created_at",
                (getattr(status, "value", status),),
            )
        return [self._session(row) for row in rows]

    def subscribe(self, subscriber: AcquisitionSubscriber) -> AcquisitionSubscriber:
        self.db.execute(
            """INSERT INTO acquisition_subscribers
               (id, acquisition_session_id, work_requirement_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(acquisition_session_id, work_requirement_id) DO UPDATE SET
                 status=excluded.status, updated_at=excluded.updated_at""",
            (
                str(subscriber.id), str(subscriber.acquisition_session_id),
                str(subscriber.work_requirement_id), subscriber.status,
                subscriber.created_at.isoformat(), utcnow().isoformat(),
            ),
        )
        return subscriber

    def update_subscriber(self, session_id, work_id, status: str) -> None:
        self.db.execute(
            "UPDATE acquisition_subscribers SET status=?, updated_at=? "
            "WHERE acquisition_session_id=? AND work_requirement_id=?",
            (status, utcnow().isoformat(), str(session_id), str(work_id)),
        )

    def subscribers(self, session_id, *, active_only: bool = False) -> list[AcquisitionSubscriber]:
        sql = "SELECT * FROM acquisition_subscribers WHERE acquisition_session_id=?"
        params: tuple = (str(session_id),)
        if active_only:
            sql += " AND status='ACTIVE'"
        sql += " ORDER BY created_at"
        return [self._subscriber(row) for row in self.db.query(sql, params)]

    def sessions_for_work(self, work_id) -> list[CapabilityAcquisitionSession]:
        rows = self.db.query(
            """SELECT s.* FROM capability_acquisition_sessions s
               JOIN acquisition_subscribers x ON x.acquisition_session_id=s.id
               WHERE x.work_requirement_id=? ORDER BY s.created_at""",
            (str(work_id),),
        )
        return [self._session(row) for row in rows]

    def save_decision(self, decision: AutonomyDecision) -> AutonomyDecision:
        self.db.execute(
            """INSERT INTO autonomy_decisions
               (id, acquisition_session_id, stage, decision, evaluated_risk,
                permissions_json, production_impact, rollback_available,
                reasons_json, policy_name, policy_version, budget_snapshot_json,
                decision_key, decided_by_process_id, reviewed_by_event_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(acquisition_session_id, decision_key) DO NOTHING""",
            (
                str(decision.id), str(decision.acquisition_session_id),
                decision.stage.value, decision.decision.value, decision.evaluated_risk,
                dumps(decision.permissions), int(decision.production_impact),
                int(decision.rollback_available), dumps(decision.reasons),
                decision.policy_name, decision.policy_version,
                dumps(decision.budget_snapshot), decision.decision_key,
                str(decision.decided_by_process_id) if decision.decided_by_process_id else None,
                str(decision.reviewed_by_event_id) if decision.reviewed_by_event_id else None,
                decision.created_at.isoformat(),
            ),
        )
        return decision

    def decisions(self, session_id) -> list[AutonomyDecision]:
        return [self._decision(row) for row in self.db.query(
            "SELECT * FROM autonomy_decisions WHERE acquisition_session_id=? ORDER BY created_at",
            (str(session_id),),
        )]

    def save_attempt(self, attempt: AcquisitionAttempt) -> AcquisitionAttempt:
        self.db.execute(
            """INSERT INTO acquisition_attempts
               (id, acquisition_session_id, attempt_type, attempt_number,
                plan_id, result_id, status, failure_reason, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(acquisition_session_id, attempt_type, attempt_number) DO UPDATE SET
                 plan_id=COALESCE(excluded.plan_id, plan_id),
                 result_id=COALESCE(excluded.result_id, result_id),
                 status=excluded.status, failure_reason=excluded.failure_reason,
                 completed_at=excluded.completed_at""",
            (
                str(attempt.id), str(attempt.acquisition_session_id),
                attempt.attempt_type, attempt.attempt_number,
                str(attempt.plan_id) if attempt.plan_id else None,
                str(attempt.result_id) if attempt.result_id else None,
                attempt.status, attempt.failure_reason, attempt.created_at.isoformat(),
                attempt.completed_at.isoformat() if attempt.completed_at else None,
            ),
        )
        return attempt

    def attempts(self, session_id, attempt_type: str | None = None) -> list[AcquisitionAttempt]:
        sql = "SELECT * FROM acquisition_attempts WHERE acquisition_session_id=?"
        params: tuple = (str(session_id),)
        if attempt_type:
            sql += " AND attempt_type=?"
            params += (attempt_type,)
        sql += " ORDER BY attempt_type, attempt_number"
        return [self._attempt(row) for row in self.db.query(sql, params)]

    @staticmethod
    def _session(row) -> CapabilityAcquisitionSession:
        return CapabilityAcquisitionSession(
            capability_gap_id=uuid.UUID(row["capability_gap_id"]),
            source_work_requirement_id=uuid.UUID(row["source_work_requirement_id"]),
            acquisition_key=row["acquisition_key"],
            target_capabilities=loads(row["target_capabilities_json"]) or [],
            status=AcquisitionStatus(row["status"]),
            current_stage=AcquisitionStage(row["current_stage"]),
            extension_proposal_id=_uuid(row["extension_proposal_id"]),
            construction_plan_id=_uuid(row["construction_plan_id"]),
            construction_result_id=_uuid(row["construction_result_id"]),
            installation_plan_id=_uuid(row["installation_plan_id"]),
            parent_session_id=_uuid(row["parent_session_id"]),
            extension_depth=int(row["extension_depth"]),
            construction_attempts=int(row["construction_attempts"]),
            installation_attempts=int(row["installation_attempts"]),
            autonomy_budget=AutonomyBudget.from_dict(loads(row["autonomy_budget_json"])),
            blocked_reason=row["blocked_reason"] or None,
            review_summary=loads(row["review_summary_json"]) or {},
            id=uuid.UUID(row["id"]), created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]), completed_at=_dt(row["completed_at"]),
        )

    @staticmethod
    def _subscriber(row) -> AcquisitionSubscriber:
        return AcquisitionSubscriber(
            acquisition_session_id=uuid.UUID(row["acquisition_session_id"]),
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            status=row["status"], id=uuid.UUID(row["id"]),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _decision(row) -> AutonomyDecision:
        return AutonomyDecision(
            acquisition_session_id=uuid.UUID(row["acquisition_session_id"]),
            stage=AcquisitionStage(row["stage"]),
            decision=AutonomyDecisionKind(row["decision"]),
            evaluated_risk=row["evaluated_risk"],
            permissions=loads(row["permissions_json"]) or [],
            production_impact=bool(row["production_impact"]),
            rollback_available=bool(row["rollback_available"]),
            reasons=loads(row["reasons_json"]) or [], policy_name=row["policy_name"],
            policy_version=row["policy_version"],
            budget_snapshot=loads(row["budget_snapshot_json"]) or {},
            decision_key=row["decision_key"],
            decided_by_process_id=_uuid(row["decided_by_process_id"]),
            reviewed_by_event_id=_uuid(row["reviewed_by_event_id"]),
            id=uuid.UUID(row["id"]), created_at=_dt(row["created_at"]),
        )

    @staticmethod
    def _attempt(row) -> AcquisitionAttempt:
        return AcquisitionAttempt(
            acquisition_session_id=uuid.UUID(row["acquisition_session_id"]),
            attempt_type=row["attempt_type"], attempt_number=int(row["attempt_number"]),
            plan_id=_uuid(row["plan_id"]), result_id=_uuid(row["result_id"]),
            status=row["status"], failure_reason=row["failure_reason"],
            id=uuid.UUID(row["id"]), created_at=_dt(row["created_at"]),
            completed_at=_dt(row["completed_at"]),
        )


__all__ = ["AutonomyStore"]
