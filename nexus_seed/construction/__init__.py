"""Sandboxed capability construction (Phase 5B).

Construction turns an approved :class:`ExtensionProposal` into auditable
artifacts and evidence inside an isolated workspace.  It deliberately stops at
``VERIFIED``: nothing in this package installs a file, registers a process, or
enables a capability.
"""

from .models import (
    ArtifactRole,
    CapabilityAssertion,
    CapabilityContract,
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
from .planner import ConstructionPlanner
from .runner import StructuredTestRunner, TestRunResult, TestRunType
from .validator import ConstructionValidation, ConstructionValidator
from .workspace import ConstructionActionBackend, SandboxWorkspaceManager
from .generator import GeneratedArtifact, LLMConstructionGenerator
from .trace import ConstructionTrace, get_construction_trace

__all__ = [
    "ArtifactRole", "CapabilityAssertion", "CapabilityContract",
    "ConstructionActionBackend", "ConstructionGrant",
    "ConstructionGrantStatus", "ConstructionPlan", "ConstructionPlanStatus",
    "ConstructionPlanner", "ConstructionResult", "ConstructionResultStatus",
    "ConstructionStep", "ConstructionStepStatus", "ConstructionStepType",
    "ConstructionValidation", "ConstructionValidator", "ExpectedArtifact",
    "ConstructionTrace", "GeneratedArtifact", "LLMConstructionGenerator",
    "NetworkPolicy", "SandboxWorkspace", "SandboxWorkspaceManager",
    "SandboxWorkspaceStatus", "StructuredTestRunner", "TestRunResult",
    "TestRunType", "VerificationCheck", "VerificationLayer",
    "VerificationStatus",
    "get_construction_trace",
]
