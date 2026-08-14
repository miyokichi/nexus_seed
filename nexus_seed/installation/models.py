"""Domain records for Phase 5C production installation and activation.

They are ordinary domain/infrastructure data, never additional core
primitives.  In particular, VERIFIED, INSTALLED and ACTIVE remain distinct.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.event import utcnow


class InstallationPlanStatus(str, Enum):
    """Durable lifecycle of one exact production promotion."""

    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    INSTALLING = "INSTALLING"
    INSTALLED = "INSTALLED"
    VERIFYING = "VERIFYING"
    ACTIVATED = "ACTIVATED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {self.ACTIVATED, self.ROLLED_BACK, self.CANCELLED}


class InstallationStepType(str, Enum):
    """Allowlisted installation operations; arbitrary commands are absent."""

    COPY_VERIFIED_ARTIFACT = "COPY_VERIFIED_ARTIFACT"
    REGISTER_COMPONENT = "REGISTER_COMPONENT"
    REGISTER_PROCESS_DEFINITION = "REGISTER_PROCESS_DEFINITION"
    REGISTER_EXTRACTOR = "REGISTER_EXTRACTOR"
    REGISTER_ADAPTER = "REGISTER_ADAPTER"
    ENABLE_COMPONENT = "ENABLE_COMPONENT"
    RUN_SMOKE_TEST = "RUN_SMOKE_TEST"
    SET_ACTIVE_VERSION = "SET_ACTIVE_VERSION"


class InstallationStepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InstallationGrantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class InstallationCheckStatus(str, Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"


class InstallationResultStatus(str, Enum):
    INSTALLED = "INSTALLED"
    ACTIVATED = "ACTIVATED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"


class ActivationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class RollbackStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InstallationDecision(str, Enum):
    REVIEW = "REVIEW"
    APPROVE = "APPROVE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class ArtifactIdentity:
    """The immutable ResourceVersion + hash selected for installation."""

    resource_id: uuid.UUID
    resource_version_id: uuid.UUID
    content_hash: str
    locator: str
    relative_path: str
    artifact_role: str
    destination: str

    def to_dict(self) -> dict:
        return {
            "resource_id": str(self.resource_id),
            "resource_version_id": str(self.resource_version_id),
            "content_hash": self.content_hash,
            "locator": self.locator,
            "relative_path": self.relative_path,
            "artifact_role": self.artifact_role,
            "destination": self.destination,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ArtifactIdentity":
        return cls(
            resource_id=uuid.UUID(value["resource_id"]),
            resource_version_id=uuid.UUID(value["resource_version_id"]),
            content_hash=str(value["content_hash"]),
            locator=str(value["locator"]),
            relative_path=str(value["relative_path"]),
            artifact_role=str(value.get("artifact_role", "")),
            destination=str(value["destination"]),
        )


@dataclass
class InstallationStep:
    installation_plan_id: uuid.UUID
    step_index: int
    step_type: InstallationStepType
    description: str = ""
    inputs: dict = field(default_factory=dict)
    required_permissions: list[str] = field(default_factory=list)
    status: InstallationStepStatus = InstallationStepStatus.PENDING
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class InstallationPlan:
    """A reviewable plan that binds a verified result to exact destinations."""

    construction_result_id: uuid.UUID
    extension_proposal_id: uuid.UUID
    capability_gap_id: uuid.UUID
    work_requirement_id: uuid.UUID
    artifact_versions: list[ArtifactIdentity]
    target_capabilities: list[str]
    production_destinations: list[str]
    registry_changes: list[dict]
    required_permissions: list[str]
    rollback_spec: dict
    component_name: str
    component_version: str
    strategy: str
    status: InstallationPlanStatus = InstallationPlanStatus.PROPOSED
    validation_reasons: list[str] = field(default_factory=list)
    context_snapshot_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    steps: list[InstallationStep] = field(default_factory=list)


@dataclass
class InstallationGrant:
    """One-plan production authority, unrelated to ConstructionGrant."""

    installation_plan_id: uuid.UUID
    allowed_artifact_hashes: list[str]
    allowed_destinations: list[str]
    allowed_registry_changes: list[dict]
    allowed_permissions: list[str]
    status: InstallationGrantStatus = InstallationGrantStatus.ACTIVE
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    revoked_at: datetime | None = None


@dataclass
class InstallationCheck:
    installation_plan_id: uuid.UUID
    check_key: str
    check_type: str
    required: bool = True
    status: InstallationCheckStatus = InstallationCheckStatus.PENDING
    evidence: dict = field(default_factory=dict)
    failure_reason: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


@dataclass
class InstallationResult:
    installation_plan_id: uuid.UUID
    status: InstallationResultStatus
    installed_artifact_versions: list[uuid.UUID] = field(default_factory=list)
    activated_capabilities: list[str] = field(default_factory=list)
    previous_state: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    failure_reason: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class InstallationDecisionRecord:
    installation_plan_id: uuid.UUID
    decision: InstallationDecision
    decided_by_process_id: uuid.UUID | None = None
    reasons: list[str] = field(default_factory=list)
    reviewed_by_event_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ActivationRecord:
    """Exact installed component exposed through the capability self-model."""

    installation_plan_id: uuid.UUID
    component_name: str
    component_version: str
    strategy: str
    definition_name: str
    definition_version: str
    capabilities: list[str]
    artifact_versions: list[uuid.UUID]
    artifact_hashes: list[str]
    installed_root: str
    previous_state: dict = field(default_factory=dict)
    status: ActivationStatus = ActivationStatus.ACTIVE
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class RollbackRecord:
    installation_plan_id: uuid.UUID
    status: RollbackStatus
    restored_state: dict = field(default_factory=dict)
    removed_destinations: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


__all__ = [name for name in globals() if name[0].isupper()]
