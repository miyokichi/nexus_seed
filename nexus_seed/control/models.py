"""Goal domain models (never Runtime/core primitives).

What remains here is the Goal and what a Goal carries.  The human *command*
vocabulary that used to live alongside it — Command, CommandResult,
HumanIdentity — is gone: people now instruct the Project Orchestrator and
decide reviews directly, so there is nothing left to parse, authorize or audit
as a command.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow


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
