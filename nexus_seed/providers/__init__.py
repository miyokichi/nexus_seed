"""Capability execution-provider federation (Phase 5E)."""

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
from .skills import DirectorySkillAdapter, SkillImporter, SkillValidationError
from .trace import ProviderTrace

__all__ = [
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
