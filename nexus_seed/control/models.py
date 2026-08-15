"""Phase 5G control-plane domain models (never Runtime/core primitives)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow


class CommandStatus(str, Enum):
    """Durable lifecycle of a human command."""

    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


class WorkPriority(str, Enum):
    """Human-facing priority names mapped to existing integer scheduling."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def scheduling_value(self) -> int:
        return {"LOW": 20, "NORMAL": 50, "HIGH": 80, "CRITICAL": 100}[self.value]


class ProviderDirectiveKind(str, Enum):
    """Whether a provider name is a hint, requirement, or exclusion."""

    PREFER = "PREFER"
    REQUIRE = "REQUIRE"
    FORBID = "FORBID"


class GoalStatus(str, Enum):
    """Lifecycle of a durable human Goal."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ACHIEVED = "ACHIEVED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {GoalStatus.ACHIEVED, GoalStatus.CANCELLED}


@dataclass(frozen=True, slots=True)
class HumanIdentity:
    """Authenticated human principal and its explicit command permissions."""

    identity_id: str
    display_name: str
    permissions: tuple[str, ...]
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def allows(self, permission: str) -> bool:
        """Return whether an exact or namespace-wildcard grant authorizes it."""

        if not self.enabled:
            return False
        grants = set(self.permissions)
        if "*" in grants or permission in grants:
            return True
        parts = permission.split(".")
        return any(".".join(parts[:index]) + ".*" in grants for index in range(1, len(parts)))


@dataclass(slots=True)
class Command:
    """One explicit or proposed control-plane request."""

    command_type: str
    issuer_identity_id: str
    source_channel: str
    source_message_id: str
    target_type: str | None = None
    target_id: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    status: CommandStatus = CommandStatus.RECEIVED
    idempotency_key: str = ""
    validation_reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    executed_at: datetime | None = None


@dataclass(slots=True)
class CommandResult:
    """Auditable explanation of what a command changed."""

    command_id: uuid.UUID
    status: CommandStatus
    affected_entities: list[dict[str, str]] = field(default_factory=list)
    emitted_events: list[str] = field(default_factory=list)
    message: str = ""
    failure_reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": str(self.command_id),
            "status": self.status.value,
            "affected_entities": self.affected_entities,
            "emitted_events": self.emitted_events,
            "message": self.message,
            "failure_reason": self.failure_reason,
            "data": self.data,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WorkConstraints:
    """Human work constraints; these may narrow but never widen safety policy."""

    cloud_forbidden: bool = False
    network_forbidden: bool = False
    production_write_forbidden: bool = False
    human_review_required: bool = False
    allowed_providers: tuple[str, ...] = ()
    forbidden_providers: tuple[str, ...] = ()
    additional: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WorkConstraints":
        raw = data or {}
        return cls(
            cloud_forbidden=bool(raw.get("cloud_forbidden", False)),
            network_forbidden=bool(raw.get("network_forbidden", False)),
            production_write_forbidden=bool(raw.get("production_write_forbidden", False)),
            human_review_required=bool(raw.get("human_review_required", False)),
            allowed_providers=tuple(str(v) for v in raw.get("allowed_providers", ()) or ()),
            forbidden_providers=tuple(str(v) for v in raw.get("forbidden_providers", ()) or ()),
            additional=tuple(str(v) for v in raw.get("additional", ()) or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cloud_forbidden": self.cloud_forbidden,
            "network_forbidden": self.network_forbidden,
            "production_write_forbidden": self.production_write_forbidden,
            "human_review_required": self.human_review_required,
            "allowed_providers": list(self.allowed_providers),
            "forbidden_providers": list(self.forbidden_providers),
            "additional": list(self.additional),
        }


@dataclass(frozen=True, slots=True)
class ProviderDirective:
    kind: ProviderDirectiveKind
    provider: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "provider": self.provider}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProviderDirective | None":
        if not data:
            return None
        return cls(ProviderDirectiveKind(str(data["kind"]).upper()), str(data["provider"]))


@dataclass(frozen=True, slots=True)
class StructuredWorkRequest:
    """Validated human input for creating one WorkRequirement."""

    objective: str
    scope: dict[str, Any] = field(default_factory=dict)
    project: str | None = None
    priority: WorkPriority = WorkPriority.NORMAL
    deadline: datetime | None = None
    input_resources: tuple[str, ...] = ()
    constraints: WorkConstraints = field(default_factory=WorkConstraints)
    completion_criteria: tuple[Any, ...] = ()
    provider_directive: ProviderDirective | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Goal:
    """Long-lived human intent that may generate several WorkRequirements."""

    title: str
    objective: str
    owner_identity_id: str
    scope: dict[str, Any] = field(default_factory=dict)
    priority: WorkPriority = WorkPriority.NORMAL
    deadline: datetime | None = None
    constraints: WorkConstraints = field(default_factory=WorkConstraints)
    success_criteria: list[Any] = field(default_factory=list)
    status: GoalStatus = GoalStatus.ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class CommandProposal:
    """Untrusted natural-language interpretation; never executable as-is."""

    proposed_command_type: str
    target_type: str | None
    target_id: str | None
    arguments: dict[str, Any]
    confidence: float
    explanation: str
    candidate_target_ids: tuple[str, ...] = ()
    id: uuid.UUID = field(default_factory=uuid.uuid4)
