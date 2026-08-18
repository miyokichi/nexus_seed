"""Capability execution-provider federation (Phase 5E)."""

from .a2a import (
    AGENT_CARD_PATH,
    A2AAgentAdapter,
    A2AAgentCard,
    A2AClient,
    A2AEndpoint,
    A2AProtocolError,
    A2ATaskUnfinished,
    a2a_provider_record,
    await_task,
    task_state,
)
from .project_agent import (
    PROJECT_AGENT_INSTRUCTION,
    PROJECT_ASSIGNMENT,
    REPLY_SCHEMA,
    A2AProjectAgentTransport,
    skill_contracts,
)
from .adapters import (
    FakeExternalAgentAdapter,
    GenericExternalAgentAdapter,
    ProviderAdapter,
    ProviderUnavailableBeforeStart,
    SkillProviderAdapter,
)
from .models import (
    DelegationRequest,
    DelegationResult,
    DelegationStatus,
    ExecutionProvider,
    ImportedSkill,
    ProviderBinding,
    ProviderHealth,
    ProviderInvocation,
    ProviderInvocationStatus,
    ProviderKind,
    ProviderSelection,
    ProviderStatus,
    SkillDescriptor,
)
from .registry import ProviderRegistry, ProviderSelector, ProviderUnavailableError
from .skills import (
    DirectorySkillAdapter,
    LoadedSkill,
    SkillCatalog,
    SkillImporter,
    SkillLoadFailure,
    SkillLoader,
    SkillValidationError,
)
from .trace import ProviderTrace

__all__ = [
    "AGENT_CARD_PATH", "A2AAgentAdapter", "A2AAgentCard", "A2AClient",
    "A2AEndpoint", "A2AProtocolError", "A2ATaskUnfinished",
    "a2a_provider_record", "await_task", "task_state",
    "A2AProjectAgentTransport", "PROJECT_AGENT_INSTRUCTION", "PROJECT_ASSIGNMENT",
    "REPLY_SCHEMA", "skill_contracts",
    "LoadedSkill", "SkillCatalog", "SkillLoadFailure", "SkillLoader",
    "DelegationRequest", "DelegationResult", "DelegationStatus",
    "ExecutionProvider", "FakeExternalAgentAdapter", "GenericExternalAgentAdapter",
    "ImportedSkill", "ProviderAdapter", "ProviderBinding", "ProviderHealth",
    "ProviderInvocation", "ProviderInvocationStatus", "ProviderKind",
    "ProviderSelection", "ProviderStatus", "ProviderUnavailableBeforeStart",
    "SkillDescriptor", "SkillProviderAdapter",
    "DirectorySkillAdapter", "ProviderRegistry", "ProviderSelector",
    "ProviderTrace", "ProviderUnavailableError", "SkillImporter",
    "SkillValidationError",
]
