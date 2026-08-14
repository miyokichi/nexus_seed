"""Self-extension — noticing what is missing, and describing how to get it.

    BLOCKED_CAPABILITY -> CapabilityGap -> AcquisitionCandidates
                       -> ExtensionProposal -> validation -> risk -> policy
                       -> human review -> APPROVED

and there it stops.  Phase 5A adds no ability to change this system: no code is
generated, no repository is written, no plugin is installed, no capability is
registered and no permission is granted (Invariant 90).  What it adds is the
ability to say precisely *what is missing, how it could be obtained, what that
would cost in risk and permissions, and whether that may proceed*.

Two rules shape everything here:

* **a deficiency is not a permission** (Invariant 84).  Needing a capability is
  not authority to acquire one, and an approved proposal is not an acquired
  capability (Invariant 89).
* **reuse before construction** (Invariant 88).  The deterministic analyzer
  looks for a disabled process, a configurable one, an already-registered
  backend, an extractor slot — in that order — before anything proposes new
  code.  A model is asked to elaborate one of those routes, never to invent one
  (Invariant 87).

Nothing in this package is a core primitive: these are domain records produced
by ordinary Processes, like ``world/``, ``work/``, ``actions/`` and
``planning/`` before them.
"""

from .analyzer import (
    CONFIGURABLE_METADATA_KEY,
    EXTENSION_HINTS_KEY,
    AcquisitionEnvironment,
    CapabilityAcquisitionAnalyzer,
    PluginCatalog,
)
from .builder import EXTENSION_SCHEMA, LLMExtensionProposer, build_proposal
from .models import (
    SOURCE_DETERMINISTIC,
    SOURCE_HUMAN,
    SOURCE_LLM,
    AcquisitionCandidate,
    AcquisitionFeasibility,
    CapabilityGap,
    CapabilityGapStatus,
    ComponentType,
    ExtensionDecision,
    ExtensionDecisionRecord,
    ExtensionProposal,
    ExtensionProposalStatus,
    ExtensionRisk,
    ExtensionStrategy,
    ProposedComponent,
    missing_key_for,
)
from .policy import ExtensionPolicy
from .strategies import (
    CRITICAL_PERMISSIONS,
    ESCALATING_PERMISSIONS,
    NEW_CODE_STRATEGIES,
    REUSE_STRATEGIES,
    STRATEGY_ORDER,
    STRATEGY_RISK,
    classify_risk,
    implied_permissions,
    strategy_rank,
)
from .trace import (
    CapabilityGapTrace,
    ExtensionTrace,
    get_capability_gap_trace,
    get_extension_trace,
)
from .validator import ExtensionValidation, ExtensionValidator

__all__ = [
    "CONFIGURABLE_METADATA_KEY",
    "CRITICAL_PERMISSIONS",
    "ESCALATING_PERMISSIONS",
    "EXTENSION_HINTS_KEY",
    "EXTENSION_SCHEMA",
    "NEW_CODE_STRATEGIES",
    "REUSE_STRATEGIES",
    "SOURCE_DETERMINISTIC",
    "SOURCE_HUMAN",
    "SOURCE_LLM",
    "STRATEGY_ORDER",
    "STRATEGY_RISK",
    "AcquisitionCandidate",
    "AcquisitionEnvironment",
    "AcquisitionFeasibility",
    "CapabilityAcquisitionAnalyzer",
    "CapabilityGap",
    "CapabilityGapStatus",
    "CapabilityGapTrace",
    "ComponentType",
    "ExtensionDecision",
    "ExtensionDecisionRecord",
    "ExtensionPolicy",
    "ExtensionProposal",
    "ExtensionProposalStatus",
    "ExtensionRisk",
    "ExtensionStrategy",
    "ExtensionTrace",
    "ExtensionValidation",
    "ExtensionValidator",
    "LLMExtensionProposer",
    "PluginCatalog",
    "ProposedComponent",
    "build_proposal",
    "classify_risk",
    "get_capability_gap_trace",
    "get_extension_trace",
    "implied_permissions",
    "missing_key_for",
    "strategy_rank",
]
