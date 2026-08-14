"""SQLite persistence for Phase 5C installation and activation records."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.event import utcnow
from ..installation.models import (
    ActivationRecord,
    ActivationStatus,
    ArtifactIdentity,
    InstallationCheck,
    InstallationCheckStatus,
    InstallationDecision,
    InstallationDecisionRecord,
    InstallationGrant,
    InstallationGrantStatus,
    InstallationPlan,
    InstallationPlanStatus,
    InstallationResult,
    InstallationResultStatus,
    InstallationStep,
    InstallationStepStatus,
    InstallationStepType,
    RollbackRecord,
    RollbackStatus,
)
from .database import Database, dumps, loads


def _uuid(value):
    return uuid.UUID(value) if value else None


def _dt(value):
    return datetime.fromisoformat(value) if value else None


class InstallationStore:
    """Store exact plans, grants, checks, outcomes and active provenance."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_plan(self, plan: InstallationPlan) -> InstallationPlan:
        existing = self.for_construction_result(plan.construction_result_id)
        if existing is not None and existing.id != plan.id:
            return existing
        self.db.execute(
            """INSERT INTO installation_plans
               (id, construction_result_id, extension_proposal_id,
                capability_gap_id, work_requirement_id, artifact_versions_json,
                target_capabilities_json, production_destinations_json,
                registry_changes_json, required_permissions_json,
                rollback_spec_json, component_name, component_version, strategy,
                status, validation_reasons_json, context_snapshot_id,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                 validation_reasons_json=excluded.validation_reasons_json,
                 updated_at=excluded.updated_at""",
            (
                str(plan.id), str(plan.construction_result_id),
                str(plan.extension_proposal_id), str(plan.capability_gap_id),
                str(plan.work_requirement_id),
                dumps([v.to_dict() for v in plan.artifact_versions]),
                dumps(plan.target_capabilities), dumps(plan.production_destinations),
                dumps(plan.registry_changes), dumps(plan.required_permissions),
                dumps(plan.rollback_spec), plan.component_name, plan.component_version,
                plan.strategy, plan.status.value, dumps(plan.validation_reasons),
                str(plan.context_snapshot_id) if plan.context_snapshot_id else None,
                plan.created_at.isoformat(), utcnow().isoformat(),
            ),
        )
        for step in plan.steps:
            self.save_step(step)
        return plan

    def update_plan_status(self, plan_id, status, *, reasons=None) -> None:
        value = getattr(status, "value", status)
        self.db.execute(
            "UPDATE installation_plans SET status=?, "
            "validation_reasons_json=COALESCE(?, validation_reasons_json), updated_at=? WHERE id=?",
            (value, dumps(reasons) if reasons is not None else None, utcnow().isoformat(), str(plan_id)),
        )

    def get_plan(self, plan_id) -> InstallationPlan | None:
        row = self.db.query_one("SELECT * FROM installation_plans WHERE id=?", (str(plan_id),))
        return self._plan(row) if row else None

    def for_construction_result(self, result_id) -> InstallationPlan | None:
        row = self.db.query_one(
            "SELECT * FROM installation_plans WHERE construction_result_id=?", (str(result_id),)
        )
        return self._plan(row) if row else None

    def all_plans(self) -> list[InstallationPlan]:
        return [self._plan(r) for r in self.db.query("SELECT * FROM installation_plans ORDER BY created_at")]

    def save_step(self, step: InstallationStep) -> InstallationStep:
        self.db.execute(
            """INSERT INTO installation_steps
               (id, installation_plan_id, step_index, step_type, description,
                inputs_json, required_permissions_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(installation_plan_id, step_index) DO UPDATE SET
                 status=excluded.status, updated_at=excluded.updated_at""",
            (str(step.id), str(step.installation_plan_id), step.step_index,
             step.step_type.value, step.description, dumps(step.inputs),
             dumps(step.required_permissions), step.status.value,
             step.created_at.isoformat(), utcnow().isoformat()),
        )
        return step

    def update_step_status(self, step_id, status) -> None:
        self.db.execute(
            "UPDATE installation_steps SET status=?, updated_at=? WHERE id=?",
            (getattr(status, "value", status), utcnow().isoformat(), str(step_id)),
        )

    def steps_for_plan(self, plan_id) -> list[InstallationStep]:
        return [self._step(r) for r in self.db.query(
            "SELECT * FROM installation_steps WHERE installation_plan_id=? ORDER BY step_index",
            (str(plan_id),),
        )]

    def save_grant(self, grant: InstallationGrant) -> InstallationGrant:
        existing = self.grant_for_plan(grant.installation_plan_id)
        if existing is not None and existing.id != grant.id:
            return existing
        self.db.execute(
            """INSERT INTO installation_grants
               (id, installation_plan_id, allowed_artifact_hashes_json,
                allowed_destinations_json, allowed_registry_changes_json,
                allowed_permissions_json, status, created_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(installation_plan_id) DO UPDATE SET
                 status=excluded.status, revoked_at=excluded.revoked_at""",
            (str(grant.id), str(grant.installation_plan_id),
             dumps(grant.allowed_artifact_hashes), dumps(grant.allowed_destinations),
             dumps(grant.allowed_registry_changes), dumps(grant.allowed_permissions),
             grant.status.value, grant.created_at.isoformat(),
             grant.revoked_at.isoformat() if grant.revoked_at else None),
        )
        return grant

    def update_grant_status(self, grant_id, status, *, revoked_at=None) -> None:
        self.db.execute(
            "UPDATE installation_grants SET status=?, revoked_at=COALESCE(?, revoked_at) WHERE id=?",
            (getattr(status, "value", status), revoked_at.isoformat() if revoked_at else None, str(grant_id)),
        )

    def grant_for_plan(self, plan_id) -> InstallationGrant | None:
        row = self.db.query_one(
            "SELECT * FROM installation_grants WHERE installation_plan_id=?", (str(plan_id),)
        )
        return self._grant(row) if row else None

    def save_check(self, check: InstallationCheck) -> InstallationCheck:
        self.db.execute(
            """INSERT INTO installation_checks
               (id, installation_plan_id, check_key, check_type, required, status,
                evidence_json, failure_reason, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(installation_plan_id, check_key) DO UPDATE SET
                 status=excluded.status, evidence_json=excluded.evidence_json,
                 failure_reason=excluded.failure_reason, completed_at=excluded.completed_at""",
            (str(check.id), str(check.installation_plan_id), check.check_key,
             check.check_type, int(check.required), check.status.value,
             dumps(check.evidence), check.failure_reason, check.created_at.isoformat(),
             check.completed_at.isoformat() if check.completed_at else None),
        )
        return check

    def checks_for_plan(self, plan_id) -> list[InstallationCheck]:
        return [self._check(r) for r in self.db.query(
            "SELECT * FROM installation_checks WHERE installation_plan_id=? ORDER BY created_at",
            (str(plan_id),),
        )]

    def save_result(self, result: InstallationResult) -> InstallationResult:
        self.db.execute(
            """INSERT INTO installation_results
               (id, installation_plan_id, status, installed_artifact_versions_json,
                activated_capabilities_json, previous_state_json, evidence_json,
                failure_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(installation_plan_id) DO UPDATE SET
                 status=excluded.status,
                 installed_artifact_versions_json=excluded.installed_artifact_versions_json,
                 activated_capabilities_json=excluded.activated_capabilities_json,
                 previous_state_json=excluded.previous_state_json,
                 evidence_json=excluded.evidence_json,
                 failure_reason=excluded.failure_reason, updated_at=excluded.updated_at""",
            (str(result.id), str(result.installation_plan_id), result.status.value,
             dumps([str(v) for v in result.installed_artifact_versions]),
             dumps(result.activated_capabilities), dumps(result.previous_state),
             dumps(result.evidence), result.failure_reason,
             result.created_at.isoformat(), utcnow().isoformat()),
        )
        return result

    def result_for_plan(self, plan_id) -> InstallationResult | None:
        row = self.db.query_one(
            "SELECT * FROM installation_results WHERE installation_plan_id=?", (str(plan_id),)
        )
        return self._result(row) if row else None

    def save_decision(self, decision: InstallationDecisionRecord) -> InstallationDecisionRecord:
        self.db.execute(
            """INSERT OR IGNORE INTO installation_decisions
               (id, installation_plan_id, decision, decided_by_process_id,
                reasons_json, reviewed_by_event_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(decision.id), str(decision.installation_plan_id), decision.decision.value,
             str(decision.decided_by_process_id) if decision.decided_by_process_id else None,
             dumps(decision.reasons),
             str(decision.reviewed_by_event_id) if decision.reviewed_by_event_id else None,
             decision.created_at.isoformat()),
        )
        return decision

    def decisions_for_plan(self, plan_id) -> list[InstallationDecisionRecord]:
        return [self._decision(r) for r in self.db.query(
            "SELECT * FROM installation_decisions WHERE installation_plan_id=? ORDER BY created_at",
            (str(plan_id),),
        )]

    def save_activation(self, record: ActivationRecord) -> ActivationRecord:
        self.db.execute(
            """INSERT INTO activation_records
               (id, installation_plan_id, component_name, component_version,
                strategy, definition_name, definition_version, capabilities_json,
                artifact_versions_json, artifact_hashes_json, installed_root,
                previous_state_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(installation_plan_id) DO UPDATE SET status=excluded.status""",
            (str(record.id), str(record.installation_plan_id), record.component_name,
             record.component_version, record.strategy, record.definition_name,
             record.definition_version, dumps(record.capabilities),
             dumps([str(v) for v in record.artifact_versions]),
             dumps(record.artifact_hashes), record.installed_root,
             dumps(record.previous_state), record.status.value, record.created_at.isoformat()),
        )
        return record

    def activation_for_plan(self, plan_id) -> ActivationRecord | None:
        row = self.db.query_one("SELECT * FROM activation_records WHERE installation_plan_id=?", (str(plan_id),))
        return self._activation(row) if row else None

    def activation_for_definition(self, name, version) -> ActivationRecord | None:
        row = self.db.query_one(
            "SELECT * FROM activation_records WHERE definition_name=? AND definition_version=? AND status='ACTIVE'",
            (name, version),
        )
        return self._activation(row) if row else None

    def active_activations(self) -> list[ActivationRecord]:
        return [self._activation(r) for r in self.db.query(
            "SELECT * FROM activation_records WHERE status='ACTIVE' ORDER BY created_at"
        )]

    def set_activation_status(self, plan_id, status) -> None:
        self.db.execute(
            "UPDATE activation_records SET status=? WHERE installation_plan_id=?",
            (getattr(status, "value", status), str(plan_id)),
        )

    def save_rollback(self, record: RollbackRecord) -> RollbackRecord:
        self.db.execute(
            """INSERT INTO rollback_records
               (id, installation_plan_id, status, restored_state_json,
                removed_destinations_json, failure_reason, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(installation_plan_id) DO UPDATE SET
                 status=excluded.status, restored_state_json=excluded.restored_state_json,
                 removed_destinations_json=excluded.removed_destinations_json,
                 failure_reason=excluded.failure_reason, completed_at=excluded.completed_at""",
            (str(record.id), str(record.installation_plan_id), record.status.value,
             dumps(record.restored_state), dumps(record.removed_destinations),
             record.failure_reason, record.created_at.isoformat(),
             record.completed_at.isoformat() if record.completed_at else None),
        )
        return record

    def rollback_for_plan(self, plan_id) -> RollbackRecord | None:
        row = self.db.query_one("SELECT * FROM rollback_records WHERE installation_plan_id=?", (str(plan_id),))
        return self._rollback(row) if row else None

    def _plan(self, row) -> InstallationPlan:
        plan = InstallationPlan(
            construction_result_id=uuid.UUID(row["construction_result_id"]),
            extension_proposal_id=uuid.UUID(row["extension_proposal_id"]),
            capability_gap_id=uuid.UUID(row["capability_gap_id"]),
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            artifact_versions=[ArtifactIdentity.from_dict(v) for v in loads(row["artifact_versions_json"]) or []],
            target_capabilities=loads(row["target_capabilities_json"]) or [],
            production_destinations=loads(row["production_destinations_json"]) or [],
            registry_changes=loads(row["registry_changes_json"]) or [],
            required_permissions=loads(row["required_permissions_json"]) or [],
            rollback_spec=loads(row["rollback_spec_json"]) or {},
            component_name=row["component_name"], component_version=row["component_version"],
            strategy=row["strategy"], status=InstallationPlanStatus(row["status"]),
            validation_reasons=loads(row["validation_reasons_json"]) or [],
            context_snapshot_id=_uuid(row["context_snapshot_id"]), id=uuid.UUID(row["id"]),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )
        plan.steps = self.steps_for_plan(plan.id)
        return plan

    @staticmethod
    def _step(row):
        return InstallationStep(
            installation_plan_id=uuid.UUID(row["installation_plan_id"]),
            step_index=row["step_index"], step_type=InstallationStepType(row["step_type"]),
            description=row["description"], inputs=loads(row["inputs_json"]) or {},
            required_permissions=loads(row["required_permissions_json"]) or [],
            status=InstallationStepStatus(row["status"]), id=uuid.UUID(row["id"]),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _grant(row):
        return InstallationGrant(
            installation_plan_id=uuid.UUID(row["installation_plan_id"]),
            allowed_artifact_hashes=loads(row["allowed_artifact_hashes_json"]) or [],
            allowed_destinations=loads(row["allowed_destinations_json"]) or [],
            allowed_registry_changes=loads(row["allowed_registry_changes_json"]) or [],
            allowed_permissions=loads(row["allowed_permissions_json"]) or [],
            status=InstallationGrantStatus(row["status"]), id=uuid.UUID(row["id"]),
            created_at=_dt(row["created_at"]), revoked_at=_dt(row["revoked_at"]),
        )

    @staticmethod
    def _check(row):
        return InstallationCheck(
            installation_plan_id=uuid.UUID(row["installation_plan_id"]),
            check_key=row["check_key"], check_type=row["check_type"], required=bool(row["required"]),
            status=InstallationCheckStatus(row["status"]), evidence=loads(row["evidence_json"]) or {},
            failure_reason=row["failure_reason"], id=uuid.UUID(row["id"]),
            created_at=_dt(row["created_at"]), completed_at=_dt(row["completed_at"]),
        )

    @staticmethod
    def _result(row):
        return InstallationResult(
            installation_plan_id=uuid.UUID(row["installation_plan_id"]),
            status=InstallationResultStatus(row["status"]),
            installed_artifact_versions=[uuid.UUID(v) for v in loads(row["installed_artifact_versions_json"]) or []],
            activated_capabilities=loads(row["activated_capabilities_json"]) or [],
            previous_state=loads(row["previous_state_json"]) or {},
            evidence=loads(row["evidence_json"]) or {}, failure_reason=row["failure_reason"],
            id=uuid.UUID(row["id"]), created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _decision(row):
        return InstallationDecisionRecord(
            installation_plan_id=uuid.UUID(row["installation_plan_id"]),
            decision=InstallationDecision(row["decision"]),
            decided_by_process_id=_uuid(row["decided_by_process_id"]),
            reasons=loads(row["reasons_json"]) or [], reviewed_by_event_id=_uuid(row["reviewed_by_event_id"]),
            id=uuid.UUID(row["id"]), created_at=_dt(row["created_at"]),
        )

    @staticmethod
    def _activation(row):
        return ActivationRecord(
            installation_plan_id=uuid.UUID(row["installation_plan_id"]),
            component_name=row["component_name"], component_version=row["component_version"],
            strategy=row["strategy"], definition_name=row["definition_name"],
            definition_version=row["definition_version"], capabilities=loads(row["capabilities_json"]) or [],
            artifact_versions=[uuid.UUID(v) for v in loads(row["artifact_versions_json"]) or []],
            artifact_hashes=loads(row["artifact_hashes_json"]) or [], installed_root=row["installed_root"],
            previous_state=loads(row["previous_state_json"]) or {}, status=ActivationStatus(row["status"]),
            id=uuid.UUID(row["id"]), created_at=_dt(row["created_at"]),
        )

    @staticmethod
    def _rollback(row):
        return RollbackRecord(
            installation_plan_id=uuid.UUID(row["installation_plan_id"]),
            status=RollbackStatus(row["status"]), restored_state=loads(row["restored_state_json"]) or {},
            removed_destinations=loads(row["removed_destinations_json"]) or [],
            failure_reason=row["failure_reason"], id=uuid.UUID(row["id"]),
            created_at=_dt(row["created_at"]), completed_at=_dt(row["completed_at"]),
        )


__all__ = ["InstallationStore"]
