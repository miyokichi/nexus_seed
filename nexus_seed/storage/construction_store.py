"""SQLite persistence for Phase 5B construction and verification records."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..construction.models import (
    ConstructionGrant,
    ConstructionGrantStatus,
    ConstructionPlan,
    ConstructionPlanStatus,
    ConstructionResult,
    ConstructionResultStatus,
    ConstructionStep,
    ConstructionStepStatus,
    ConstructionStepType,
    ExpectedArtifact,
    NetworkPolicy,
    SandboxWorkspace,
    SandboxWorkspaceStatus,
    VerificationCheck,
    VerificationLayer,
    VerificationStatus,
)
from ..core.event import utcnow
from .database import Database, dumps, loads


def _uuid(value):
    return uuid.UUID(value) if value else None


class ConstructionStore:
    """Stores plans, step progress, grants, evidence and terminal results."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_plan(self, plan: ConstructionPlan) -> ConstructionPlan:
        existing = self.get_by_proposal_attempt(plan.extension_proposal_id, plan.attempt)
        if existing is not None and existing.id != plan.id:
            return existing
        self.db.execute(
            """
            INSERT INTO construction_plans
              (id, extension_proposal_id, capability_gap_id, work_requirement_id,
               fingerprint, attempt, target_capabilities_json,
               expected_artifacts_json, verification_requirements_json,
               sandbox_requirements_json, required_permissions_json, status,
               context_snapshot_id, llm_invocation_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status, llm_invocation_id=excluded.llm_invocation_id,
              updated_at=excluded.updated_at
            """,
            (
                str(plan.id), str(plan.extension_proposal_id), str(plan.capability_gap_id),
                str(plan.work_requirement_id), plan.fingerprint, plan.attempt,
                dumps(plan.target_capabilities),
                dumps([a.to_dict() for a in plan.expected_artifacts]),
                dumps(plan.verification_requirements), dumps(plan.sandbox_requirements),
                dumps(plan.required_permissions), plan.status.value,
                str(plan.context_snapshot_id) if plan.context_snapshot_id else None,
                str(plan.llm_invocation_id) if plan.llm_invocation_id else None,
                plan.created_at.isoformat(), utcnow().isoformat(),
            ),
        )
        for step in plan.steps:
            self.save_step(step)
        return plan

    def update_plan_status(self, plan_id, status) -> None:
        value = getattr(status, "value", status)
        self.db.execute(
            "UPDATE construction_plans SET status=?, updated_at=? WHERE id=?",
            (value, utcnow().isoformat(), str(plan_id)),
        )

    def get_plan(self, plan_id) -> ConstructionPlan | None:
        row = self.db.query_one("SELECT * FROM construction_plans WHERE id=?", (str(plan_id),))
        return self._plan(row) if row else None

    def get_by_proposal_attempt(self, proposal_id, attempt: int) -> ConstructionPlan | None:
        row = self.db.query_one(
            "SELECT * FROM construction_plans WHERE extension_proposal_id=? AND attempt=?",
            (str(proposal_id), attempt),
        )
        return self._plan(row) if row else None

    def active_for_proposal(self, proposal_id) -> ConstructionPlan | None:
        terminal = tuple(s.value for s in (
            ConstructionPlanStatus.VERIFIED, ConstructionPlanStatus.FAILED,
            ConstructionPlanStatus.CANCELLED, ConstructionPlanStatus.BLOCKED,
        ))
        row = self.db.query_one(
            "SELECT * FROM construction_plans WHERE extension_proposal_id=? "
            "AND status NOT IN (?, ?, ?, ?) ORDER BY attempt DESC LIMIT 1",
            (str(proposal_id), *terminal),
        )
        return self._plan(row) if row else None

    def plans_for_proposal(self, proposal_id) -> list[ConstructionPlan]:
        return [self._plan(r) for r in self.db.query(
            "SELECT * FROM construction_plans WHERE extension_proposal_id=? ORDER BY attempt",
            (str(proposal_id),),
        )]

    def all_plans(self) -> list[ConstructionPlan]:
        return [self._plan(r) for r in self.db.query("SELECT * FROM construction_plans ORDER BY created_at")]

    def save_step(self, step: ConstructionStep) -> ConstructionStep:
        self.db.execute(
            """
            INSERT INTO construction_steps
              (id, plan_id, step_index, step_type, description, inputs_json,
               expected_outputs_json, required_permissions_json, status,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id, step_index) DO UPDATE SET
              status=excluded.status, updated_at=excluded.updated_at
            """,
            (str(step.id), str(step.plan_id), step.step_index, step.step_type.value,
             step.description, dumps(step.inputs), dumps(step.expected_outputs),
             dumps(step.required_permissions), step.status.value,
             step.created_at.isoformat(), utcnow().isoformat()),
        )
        return step

    def update_step_status(self, step_id, status) -> None:
        self.db.execute(
            "UPDATE construction_steps SET status=?, updated_at=? WHERE id=?",
            (getattr(status, "value", status), utcnow().isoformat(), str(step_id)),
        )

    def steps_for_plan(self, plan_id) -> list[ConstructionStep]:
        rows = self.db.query(
            "SELECT * FROM construction_steps WHERE plan_id=? ORDER BY step_index", (str(plan_id),)
        )
        return [self._step(r) for r in rows]

    def save_workspace(self, workspace: SandboxWorkspace) -> SandboxWorkspace:
        existing = self.get_workspace_for_plan(workspace.construction_plan_id)
        if existing is not None and existing.id != workspace.id:
            return existing
        self.db.execute(
            """
            INSERT INTO sandbox_workspaces
              (id, construction_plan_id, root_locator, status, max_files,
               max_file_bytes, max_total_bytes, timeout_seconds, created_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(construction_plan_id) DO UPDATE SET
              status=excluded.status, closed_at=excluded.closed_at
            """,
            (str(workspace.id), str(workspace.construction_plan_id), workspace.root_locator,
             workspace.status.value, workspace.max_files, workspace.max_file_bytes,
             workspace.max_total_bytes, workspace.timeout_seconds,
             workspace.created_at.isoformat(),
             workspace.closed_at.isoformat() if workspace.closed_at else None),
        )
        return workspace

    def update_workspace_status(self, workspace_id, status, *, closed_at=None) -> None:
        self.db.execute(
            "UPDATE sandbox_workspaces SET status=?, closed_at=COALESCE(?, closed_at) WHERE id=?",
            (getattr(status, "value", status), closed_at.isoformat() if closed_at else None, str(workspace_id)),
        )

    def get_workspace(self, workspace_id) -> SandboxWorkspace | None:
        row = self.db.query_one("SELECT * FROM sandbox_workspaces WHERE id=?", (str(workspace_id),))
        return self._workspace(row) if row else None

    def get_workspace_for_plan(self, plan_id) -> SandboxWorkspace | None:
        row = self.db.query_one(
            "SELECT * FROM sandbox_workspaces WHERE construction_plan_id=?", (str(plan_id),)
        )
        return self._workspace(row) if row else None

    def save_grant(self, grant: ConstructionGrant) -> ConstructionGrant:
        existing = self.get_grant_for_plan(grant.construction_plan_id)
        if existing is not None and existing.id != grant.id:
            return existing
        self.db.execute(
            """
            INSERT INTO construction_grants
              (id, construction_plan_id, workspace_id, allowed_permissions_json,
               allowed_root, network_policy, process_policy_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(construction_plan_id) DO UPDATE SET status=excluded.status
            """,
            (str(grant.id), str(grant.construction_plan_id), str(grant.workspace_id),
             dumps(grant.allowed_permissions), grant.allowed_root,
             grant.network_policy.value, dumps(grant.process_policy),
             grant.status.value, grant.created_at.isoformat()),
        )
        return grant

    def update_grant_status(self, grant_id, status) -> None:
        self.db.execute(
            "UPDATE construction_grants SET status=? WHERE id=?",
            (getattr(status, "value", status), str(grant_id)),
        )

    def get_grant_for_plan(self, plan_id) -> ConstructionGrant | None:
        row = self.db.query_one(
            "SELECT * FROM construction_grants WHERE construction_plan_id=?", (str(plan_id),)
        )
        return self._grant(row) if row else None

    def save_check(self, check: VerificationCheck) -> VerificationCheck:
        existing = self.db.query_one(
            "SELECT id FROM verification_checks WHERE construction_plan_id=? AND check_key=?",
            (str(check.construction_plan_id), check.check_key),
        )
        if existing and existing["id"] != str(check.id):
            return self.get_check(existing["id"])
        self.db.execute(
            """
            INSERT INTO verification_checks
              (id, construction_plan_id, layer, check_type, check_key, required,
               status, evidence_json, failure_reason, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET status=excluded.status,
              evidence_json=excluded.evidence_json,
              failure_reason=excluded.failure_reason,
              completed_at=excluded.completed_at
            """,
            (str(check.id), str(check.construction_plan_id), check.layer.value,
             check.check_type, check.check_key, int(check.required), check.status.value,
             dumps(check.evidence), check.failure_reason, check.created_at.isoformat(),
             check.completed_at.isoformat() if check.completed_at else None),
        )
        return check

    def get_check(self, check_id) -> VerificationCheck | None:
        row = self.db.query_one("SELECT * FROM verification_checks WHERE id=?", (str(check_id),))
        return self._check(row) if row else None

    def checks_for_plan(self, plan_id) -> list[VerificationCheck]:
        return [self._check(r) for r in self.db.query(
            "SELECT * FROM verification_checks WHERE construction_plan_id=? ORDER BY created_at",
            (str(plan_id),),
        )]

    def save_result(self, result: ConstructionResult) -> ConstructionResult:
        existing = self.result_for_plan(result.construction_plan_id)
        if existing is not None and existing.id != result.id:
            return existing
        self.db.execute(
            """
            INSERT INTO construction_results
              (id, construction_plan_id, extension_proposal_id, status,
               artifact_resource_ids_json, verification_check_ids_json,
               provided_capabilities_json, evidence_json, failure_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (str(result.id), str(result.construction_plan_id), str(result.extension_proposal_id),
             result.status.value, dumps([str(v) for v in result.artifact_resource_ids]),
             dumps([str(v) for v in result.verification_check_ids]),
             dumps(result.provided_capabilities), dumps(result.evidence),
             result.failure_reason, result.created_at.isoformat()),
        )
        return result

    def result_for_plan(self, plan_id) -> ConstructionResult | None:
        row = self.db.query_one(
            "SELECT * FROM construction_results WHERE construction_plan_id=?", (str(plan_id),)
        )
        return self._result(row) if row else None

    def get_result(self, result_id) -> ConstructionResult | None:
        """Return a construction result by its own durable identity."""
        row = self.db.query_one(
            "SELECT * FROM construction_results WHERE id=?", (str(result_id),)
        )
        return self._result(row) if row else None

    @staticmethod
    def _step(row) -> ConstructionStep:
        return ConstructionStep(
            plan_id=uuid.UUID(row["plan_id"]), step_index=row["step_index"],
            step_type=ConstructionStepType(row["step_type"]), description=row["description"],
            inputs=loads(row["inputs_json"]) or {}, expected_outputs=loads(row["expected_outputs_json"]) or [],
            required_permissions=loads(row["required_permissions_json"]) or [],
            status=ConstructionStepStatus(row["status"]), id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _workspace(row) -> SandboxWorkspace:
        return SandboxWorkspace(
            construction_plan_id=uuid.UUID(row["construction_plan_id"]),
            root_locator=row["root_locator"], status=SandboxWorkspaceStatus(row["status"]),
            max_files=row["max_files"], max_file_bytes=row["max_file_bytes"],
            max_total_bytes=row["max_total_bytes"], timeout_seconds=row["timeout_seconds"],
            id=uuid.UUID(row["id"]), created_at=datetime.fromisoformat(row["created_at"]),
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        )

    @staticmethod
    def _grant(row) -> ConstructionGrant:
        return ConstructionGrant(
            construction_plan_id=uuid.UUID(row["construction_plan_id"]),
            workspace_id=uuid.UUID(row["workspace_id"]),
            allowed_permissions=loads(row["allowed_permissions_json"]) or [],
            allowed_root=row["allowed_root"], network_policy=NetworkPolicy(row["network_policy"]),
            process_policy=loads(row["process_policy_json"]) or {},
            status=ConstructionGrantStatus(row["status"]), id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _check(row) -> VerificationCheck:
        return VerificationCheck(
            construction_plan_id=uuid.UUID(row["construction_plan_id"]),
            layer=VerificationLayer(row["layer"]), check_type=row["check_type"],
            check_key=row["check_key"], required=bool(row["required"]),
            status=VerificationStatus(row["status"]), evidence=loads(row["evidence_json"]) or {},
            failure_reason=row["failure_reason"], id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        )

    @staticmethod
    def _result(row) -> ConstructionResult:
        return ConstructionResult(
            construction_plan_id=uuid.UUID(row["construction_plan_id"]),
            extension_proposal_id=uuid.UUID(row["extension_proposal_id"]),
            status=ConstructionResultStatus(row["status"]),
            artifact_resource_ids=[uuid.UUID(v) for v in loads(row["artifact_resource_ids_json"]) or []],
            verification_check_ids=[uuid.UUID(v) for v in loads(row["verification_check_ids_json"]) or []],
            provided_capabilities=loads(row["provided_capabilities_json"]) or [],
            evidence=loads(row["evidence_json"]) or {}, failure_reason=row["failure_reason"],
            id=uuid.UUID(row["id"]), created_at=datetime.fromisoformat(row["created_at"]),
        )

    # Public reads return complete aggregate plans.
    def _plan(self, row):  # type: ignore[override]
        plan = ConstructionStore._plan_row(row)
        plan.steps = self.steps_for_plan(plan.id)
        return plan

    @staticmethod
    def _plan_row(row) -> ConstructionPlan:
        plan = ConstructionPlan(
            extension_proposal_id=uuid.UUID(row["extension_proposal_id"]),
            capability_gap_id=uuid.UUID(row["capability_gap_id"]),
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            target_capabilities=loads(row["target_capabilities_json"]) or [],
            expected_artifacts=[ExpectedArtifact.from_dict(v) for v in loads(row["expected_artifacts_json"]) or []],
            verification_requirements=loads(row["verification_requirements_json"]) or [],
            sandbox_requirements=loads(row["sandbox_requirements_json"]) or {},
            required_permissions=loads(row["required_permissions_json"]) or [],
            status=ConstructionPlanStatus(row["status"]),
            context_snapshot_id=_uuid(row["context_snapshot_id"]),
            llm_invocation_id=_uuid(row["llm_invocation_id"]), attempt=row["attempt"],
            id=uuid.UUID(row["id"]), created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        return plan


__all__ = ["ConstructionStore"]
