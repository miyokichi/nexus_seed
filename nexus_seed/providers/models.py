"""Phase 5E provider federation domain models (never core primitives)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.event import utcnow


class ProviderKind(str, Enum):
    """Where a ProcessDefinition activation is executed."""
    INTERNAL = "INTERNAL"
    EXTERNAL_SKILL = "EXTERNAL_SKILL"
    EXTERNAL_AGENT = "EXTERNAL_AGENT"


class ProviderStatus(str, Enum):
    """Administrative availability of an execution provider."""
    ACTIVE = "ACTIVE"
    UNAVAILABLE = "UNAVAILABLE"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


class ProviderHealth(str, Enum):
    """Observed operational health, separate from administrative status."""
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderInvocationStatus(str, Enum):
    """Durable lifecycle of one external delegation attempt."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DelegationStatus(str, Enum):
    """Status returned through the provider-neutral adapter contract."""
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"


LOCAL_PROVIDER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "nexus-seed:provider:local-runtime")


@dataclass
class ExecutionProvider:
    """An operational executor for a semantic ProcessDefinition."""
    name: str
    version: str
    kind: ProviderKind
    adapter_name: str
    status: ProviderStatus = ProviderStatus.ACTIVE
    health: ProviderHealth = ProviderHealth.UNKNOWN
    adapter_config: dict = field(default_factory=dict)
    declared_permissions: tuple[str, ...] = ()
    priority: int = 0
    estimated_cost: float | None = None
    estimated_latency: float | None = None
    trust_level: float = 0.5
    metadata: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def operational(self) -> bool:
        """Whether selection may currently consider this provider."""
        return (
            self.status in {ProviderStatus.ACTIVE, ProviderStatus.DEGRADED}
            and self.health is not ProviderHealth.UNAVAILABLE
        )


@dataclass
class ProviderBinding:
    """A versioned ProcessDefinition-to-provider execution binding."""
    process_definition_name: str
    process_definition_version: str
    provider_id: uuid.UUID
    priority: int = 0
    enabled: bool = True
    required_permissions: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def definition_key(self) -> tuple[str, str]:
        return self.process_definition_name, self.process_definition_version


@dataclass
class ProviderSelection:
    """An append-only audit record of one deterministic selection decision."""
    process_instance_id: uuid.UUID
    activation_id: str
    process_definition_name: str
    process_definition_version: str
    provider_id: uuid.UUID | None
    provider_binding_id: uuid.UUID | None
    eligible_provider_ids: list[uuid.UUID] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    selected_at: datetime = field(default_factory=utcnow)


@dataclass
class DelegationRequest:
    """The bounded Context and typed contract sent to an external executor."""
    invocation_id: uuid.UUID
    process_instance_id: uuid.UUID
    process_definition: dict
    objective: str
    idempotency_key: str
    plan_node_id: uuid.UUID | None = None
    required_capabilities: list[str] = field(default_factory=list)
    typed_inputs: dict = field(default_factory=dict)
    relevant_context: dict = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    allowed_permissions: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "invocation_id": str(self.invocation_id),
            "process_instance_id": str(self.process_instance_id),
            "plan_node_id": str(self.plan_node_id) if self.plan_node_id else None,
            "process_definition": self.process_definition,
            "required_capabilities": self.required_capabilities,
            "objective": self.objective,
            "typed_inputs": self.typed_inputs,
            "relevant_context": self.relevant_context,
            "constraints": self.constraints,
            "allowed_permissions": self.allowed_permissions,
            "idempotency_key": self.idempotency_key,
            "metadata": self.metadata,
        }


@dataclass
class DelegationResult:
    """Structured external output that remains behind NEXUS safety boundaries."""
    invocation_id: uuid.UUID
    status: DelegationStatus
    typed_outputs: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    error: str | None = None
    external_run_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "invocation_id": str(self.invocation_id),
            "status": self.status.value,
            "typed_outputs": self.typed_outputs,
            "artifacts": self.artifacts,
            "proposals": self.proposals,
            "error": self.error,
            "external_run_id": self.external_run_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict, *, invocation_id=None) -> "DelegationResult":
        return cls(
            invocation_id=invocation_id or uuid.UUID(str(data["invocation_id"])),
            status=DelegationStatus(str(data.get("status", "FAILED")).upper()),
            typed_outputs=list(data.get("typed_outputs") or []),
            artifacts=list(data.get("artifacts") or []),
            proposals=list(data.get("proposals") or []),
            error=data.get("error"),
            external_run_id=data.get("external_run_id"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ProviderInvocation:
    """Durable, idempotent journal record for external execution."""
    provider_id: uuid.UUID
    provider_binding_id: uuid.UUID
    process_instance_id: uuid.UUID
    request_snapshot: dict
    idempotency_key: str
    status: ProviderInvocationStatus = ProviderInvocationStatus.PENDING
    plan_node_id: uuid.UUID | None = None
    external_run_id: str | None = None
    attempt: int = 1
    result_snapshot: dict | None = None
    error: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


@dataclass
class SkillDescriptor:
    """Source-neutral description of an inspected external Skill package."""
    name: str
    version: str
    description: str
    provided_capabilities: list[dict]
    input_ports: list[str]
    output_ports: list[str]
    required_permissions: list[str]
    instructions: str
    resources: list[str] = field(default_factory=list)
    execution_kind: str = "EXTERNAL_SKILL"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "provided_capabilities": self.provided_capabilities,
            "input_ports": self.input_ports,
            "output_ports": self.output_ports,
            "required_permissions": self.required_permissions,
            "instructions": self.instructions,
            "resources": self.resources,
            "execution_kind": self.execution_kind,
            "metadata": self.metadata,
        }


@dataclass
class ImportedSkill:
    """Provenance linking Skill source and descriptor to standard records."""
    source: str
    descriptor: SkillDescriptor
    status: str
    provider_id: uuid.UUID | None = None
    process_definition_name: str | None = None
    process_definition_version: str | None = None
    reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


def local_provider() -> ExecutionProvider:
    """Return the stable built-in provider used by existing local handlers."""
    return ExecutionProvider(
        id=LOCAL_PROVIDER_ID,
        name="local_runtime",
        version="1",
        kind=ProviderKind.INTERNAL,
        adapter_name="internal",
        health=ProviderHealth.HEALTHY,
        trust_level=1.0,
        metadata={"built_in": True},
    )
