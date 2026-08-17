"""Capability execution-provider federation (Phase 5E)."""

from .a2a import (
    AGENT_CARD_PATH,
    A2AAgentAdapter,
    A2AAgentCard,
    A2AClient,
    A2AEndpoint,
    A2AProtocolError,
    a2a_provider_record,
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
    "A2AEndpoint", "A2AProtocolError", "a2a_provider_record",
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
