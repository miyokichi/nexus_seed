"""Durable domain records for the bounded capability-acquisition loop.

These records coordinate Phase 5A/5B/5C; they do not replace any of their
validators, grants, actions, evidence, or rollback records.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.event import utcnow


class AcquisitionStatus(str, Enum):
    """Durable lifecycle of one logical acquisition."""

    OPEN = "OPEN"
    ANALYZING = "ANALYZING"
    WAITING_REVIEW = "WAITING_REVIEW"
    CONSTRUCTING = "CONSTRUCTING"
    VERIFYING = "VERIFYING"
    INSTALLING = "INSTALLING"
    ACTIVATING = "ACTIVATING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.BLOCKED, self.FAILED, self.CANCELLED}


class AcquisitionStage(str, Enum):
    """Boundary currently being coordinated."""

    GAP = "GAP"
    EXTENSION = "EXTENSION"
    CONSTRUCTION = "CONSTRUCTION"
    INSTALLATION = "INSTALLATION"
    ACTIVATION = "ACTIVATION"
    RECONCILIATION = "RECONCILIATION"
    COMPLETED = "COMPLETED"


class AutonomyDecisionKind(str, Enum):
    """The only policy outcomes; FORBIDDEN is not human-overridable."""

    AUTO = "AUTO"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FORBIDDEN = "FORBIDDEN"


class ApprovalSource(str, Enum):
    """Who supplied an approval routed through an existing review boundary."""

    HUMAN = "HUMAN"
    AUTONOMY_POLICY = "AUTONOMY_POLICY"


@dataclass(frozen=True)
class AutonomyBudget:
    """Hard limits for recursive and repeated acquisition work."""

    max_extension_depth: int = 2
    max_extensions_per_work: int = 3
    max_construction_attempts: int = 2
    max_installation_attempts: int = 2
    max_total_cost: float | None = None
    max_elapsed_seconds: float | None = None
    allowed_risk: str = "HIGH"

    def to_dict(self) -> dict:
        return {
            "max_extension_depth": self.max_extension_depth,
            "max_extensions_per_work": self.max_extensions_per_work,
            "max_construction_attempts": self.max_construction_attempts,
            "max_installation_attempts": self.max_installation_attempts,
            "max_total_cost": self.max_total_cost,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "allowed_risk": self.allowed_risk,
        }

    @classmethod
    def from_dict(cls, value: dict | None) -> "AutonomyBudget":
        value = value if isinstance(value, dict) else {}
        return cls(
            max_extension_depth=max(0, int(value.get("max_extension_depth", 2))),
            max_extensions_per_work=max(1, int(value.get("max_extensions_per_work", 3))),
            max_construction_attempts=max(1, int(value.get("max_construction_attempts", 2))),
            max_installation_attempts=max(1, int(value.get("max_installation_attempts", 2))),
            max_total_cost=(float(value["max_total_cost"]) if value.get("max_total_cost") is not None else None),
            max_elapsed_seconds=(float(value["max_elapsed_seconds"]) if value.get("max_elapsed_seconds") is not None else None),
            allowed_risk=str(value.get("allowed_risk", "HIGH")).upper(),
        )


def acquisition_key_for(requirements, *, intent: str = "acquire") -> str:
    """Return the order-independent, version-aware identity of an acquisition."""

    normalized = sorted(
        (
            {
                "name": str(
                    getattr(
                        item,
                        "name",
                        item.get("name") if isinstance(item, dict) else item,
                    )
                ),
                "version_constraint": (
                    getattr(item, "version_constraint", None)
                    if not isinstance(item, dict)
                    else item.get("version_constraint")
                ),
            }
            for item in requirements
        ),
        key=lambda item: (item["name"], item.get("version_constraint") or ""),
    )
    raw = json.dumps({"intent": intent, "requirements": normalized}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class CapabilityAcquisitionSession:
    """One durable, shareable attempt to acquire a logical competence."""

    capability_gap_id: uuid.UUID
    source_work_requirement_id: uuid.UUID
    acquisition_key: str
    target_capabilities: list[dict] = field(default_factory=list)
    status: AcquisitionStatus = AcquisitionStatus.OPEN
    current_stage: AcquisitionStage = AcquisitionStage.GAP
    extension_proposal_id: uuid.UUID | None = None
    construction_plan_id: uuid.UUID | None = None
    construction_result_id: uuid.UUID | None = None
    installation_plan_id: uuid.UUID | None = None
    parent_session_id: uuid.UUID | None = None
    extension_depth: int = 0
    construction_attempts: int = 0
    installation_attempts: int = 0
    autonomy_budget: AutonomyBudget = field(default_factory=AutonomyBudget)
    blocked_reason: str | None = None
    review_summary: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


@dataclass
class AutonomyDecision:
    """Append-only explanation of one deterministic policy judgement."""

    acquisition_session_id: uuid.UUID
    stage: AcquisitionStage
    decision: AutonomyDecisionKind
    evaluated_risk: str
    permissions: list[str] = field(default_factory=list)
    production_impact: bool = False
    rollback_available: bool = False
    reasons: list[str] = field(default_factory=list)
    policy_name: str = "default_conservative"
    policy_version: str = "1"
    budget_snapshot: dict = field(default_factory=dict)
    decision_key: str = ""
    decided_by_process_id: uuid.UUID | None = None
    reviewed_by_event_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.decision_key:
            self.decision_key = f"{self.stage.value}:{self.policy_name}:{self.policy_version}"


@dataclass
class AcquisitionSubscriber:
    """A WorkRequirement waiting on a shared acquisition."""

    acquisition_session_id: uuid.UUID
    work_requirement_id: uuid.UUID
    status: str = "ACTIVE"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class AcquisitionAttempt:
    """One logical construction or installation attempt."""

    acquisition_session_id: uuid.UUID
    attempt_type: str
    attempt_number: int
    plan_id: uuid.UUID | None = None
    result_id: uuid.UUID | None = None
    status: str = "STARTED"
    failure_reason: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


__all__ = [
    "AcquisitionAttempt", "AcquisitionStage", "AcquisitionStatus",
    "AcquisitionSubscriber", "ApprovalSource", "AutonomyBudget",
    "AutonomyDecision", "AutonomyDecisionKind", "CapabilityAcquisitionSession",
    "acquisition_key_for",
]
