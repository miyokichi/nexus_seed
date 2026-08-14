"""Domain records for Phase 5B sandboxed construction.

These records are not core primitives.  They describe how an approved
extension is built and verified without changing the production system.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow


class ConstructionPlanStatus(str, Enum):
    """Durable lifecycle of a construction plan."""
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"

    @property
    def terminal(self) -> bool:
        return self in {self.VERIFIED, self.FAILED, self.CANCELLED, self.BLOCKED}


class ConstructionStepType(str, Enum):
    """Allowlisted operations a plan may describe."""
    CREATE_FILE = "CREATE_FILE"
    MODIFY_SANDBOX_FILE = "MODIFY_SANDBOX_FILE"
    GENERATE_CODE = "GENERATE_CODE"
    GENERATE_CONFIGURATION = "GENERATE_CONFIGURATION"
    RUN_STATIC_CHECK = "RUN_STATIC_CHECK"
    RUN_TEST = "RUN_TEST"
    INSPECT_ARTIFACT = "INSPECT_ARTIFACT"


class ConstructionStepStatus(str, Enum):
    """Durable progress of one logical construction step."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ArtifactRole(str, Enum):
    """Known roles accepted from a code generator."""
    IMPLEMENTATION = "implementation"
    TEST = "test"
    CONFIGURATION = "configuration"
    FIXTURE = "fixture"
    MANIFEST = "manifest"


class NetworkPolicy(str, Enum):
    """Construction network policy; Phase 5B only permits DENY."""
    DENY = "DENY"


class SandboxWorkspaceStatus(str, Enum):
    """Lifecycle of an isolated workspace."""
    READY = "READY"
    ACTIVE = "ACTIVE"
    SEALED = "SEALED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class ConstructionGrantStatus(str, Enum):
    """Lifecycle of temporary, construction-scoped authority."""
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class VerificationLayer(str, Enum):
    """The three independent layers needed for capability evidence."""
    STRUCTURAL = "STRUCTURAL"
    STATIC = "STATIC"
    BEHAVIOR = "BEHAVIOR"


class VerificationStatus(str, Enum):
    """Outcome of one structured verification check."""
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class ConstructionResultStatus(str, Enum):
    """Terminal outcome of one construction attempt."""
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class CapabilityAssertion(str, Enum):
    """Allowlisted, data-only assertions in a CapabilityContract."""
    NO_EXCEPTION = "NO_EXCEPTION"
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    OUTPUT_TYPE_EQUALS = "OUTPUT_TYPE_EQUALS"
    JSON_FIELD_EQUALS = "JSON_FIELD_EQUALS"
    CONTENT_CONTAINS = "CONTENT_CONTAINS"


@dataclass(frozen=True)
class ExpectedArtifact:
    """One path and role a validated plan requires."""
    relative_path: str
    artifact_role: str
    required: bool = True

    def to_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "artifact_role": self.artifact_role,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ExpectedArtifact":
        if not isinstance(value, dict):
            raise ValueError("expected artifact must be an object")
        return cls(
            relative_path=str(value.get("relative_path", "")),
            artifact_role=str(value.get("artifact_role", "")),
            required=bool(value.get("required", True)),
        )


@dataclass
class ConstructionStep:
    """A logical position in a plan, not an OS process or shell command."""
    plan_id: uuid.UUID
    step_index: int
    step_type: ConstructionStepType
    description: str = ""
    inputs: dict = field(default_factory=dict)
    expected_outputs: list[dict] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    status: ConstructionStepStatus = ConstructionStepStatus.PENDING
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class ConstructionPlan:
    """The validated HOW for one approved ExtensionProposal."""
    extension_proposal_id: uuid.UUID
    capability_gap_id: uuid.UUID
    work_requirement_id: uuid.UUID
    target_capabilities: list[str] = field(default_factory=list)
    steps: list[ConstructionStep] = field(default_factory=list)
    expected_artifacts: list[ExpectedArtifact] = field(default_factory=list)
    verification_requirements: list[dict] = field(default_factory=list)
    sandbox_requirements: dict = field(default_factory=dict)
    required_permissions: list[str] = field(default_factory=list)
    status: ConstructionPlanStatus = ConstructionPlanStatus.PROPOSED
    context_snapshot_id: uuid.UUID | None = None
    llm_invocation_id: uuid.UUID | None = None
    attempt: int = 1
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def fingerprint(self) -> str:
        payload = {
            "proposal": str(self.extension_proposal_id),
            "attempt": self.attempt,
            "targets": sorted(self.target_capabilities),
            "steps": [
                [s.step_index, s.step_type.value, s.inputs, s.expected_outputs]
                for s in self.steps
            ],
            "artifacts": [a.to_dict() for a in self.expected_artifacts],
            "verification": self.verification_requirements,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class SandboxWorkspace:
    """A dedicated root plus resource and time limits for one plan."""
    construction_plan_id: uuid.UUID
    root_locator: str
    status: SandboxWorkspaceStatus = SandboxWorkspaceStatus.READY
    max_files: int = 32
    max_file_bytes: int = 256_000
    max_total_bytes: int = 1_000_000
    timeout_seconds: float = 10.0
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    closed_at: datetime | None = None


@dataclass
class ConstructionGrant:
    """Temporary sandbox authority, separate from global permissions."""
    construction_plan_id: uuid.UUID
    workspace_id: uuid.UUID
    allowed_permissions: list[str]
    allowed_root: str
    network_policy: NetworkPolicy = NetworkPolicy.DENY
    process_policy: dict = field(default_factory=lambda: {"structured_runner_only": True})
    status: ConstructionGrantStatus = ConstructionGrantStatus.ACTIVE
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class CapabilityContract:
    """Structured fixture and assertions proving target behavior."""
    capability_name: str
    input_fixture: dict = field(default_factory=dict)
    expected_output_type: str | None = None
    assertions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "capability_name": self.capability_name,
            "input_fixture": dict(self.input_fixture),
            "expected_output_type": self.expected_output_type,
            "assertions": [dict(a) for a in self.assertions],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityContract":
        if not isinstance(value, dict):
            raise ValueError("capability contract must be an object")
        return cls(
            capability_name=str(value.get("capability_name", "")),
            input_fixture=dict(value.get("input_fixture") or {}),
            expected_output_type=value.get("expected_output_type"),
            assertions=[dict(a) for a in value.get("assertions", []) if isinstance(a, dict)],
        )


@dataclass
class VerificationCheck:
    """Durable evidence for one required verification layer."""
    construction_plan_id: uuid.UUID
    layer: VerificationLayer
    check_type: str
    check_key: str
    required: bool = True
    status: VerificationStatus = VerificationStatus.PENDING
    evidence: dict = field(default_factory=dict)
    failure_reason: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


@dataclass
class ConstructionResult:
    """Terminal evidence bundle; VERIFIED never means ACTIVE."""
    construction_plan_id: uuid.UUID
    extension_proposal_id: uuid.UUID
    status: ConstructionResultStatus
    artifact_resource_ids: list[uuid.UUID] = field(default_factory=list)
    verification_check_ids: list[uuid.UUID] = field(default_factory=list)
    provided_capabilities: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    failure_reason: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
