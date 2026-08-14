"""Capability layer — the system's model of what it can do.

    WorkRequirement -> required capabilities
                    -> CapabilityRegistry -> candidate definitions
                    -> CapabilityMatcher  -> one process, or a recorded gap

Domain/registry data, not a core primitive.  Distinct from Phase 3C's
``BackendCapabilities``, which describes what a *tool* can mechanically do.
"""

from .matcher import (
    ENABLED_METADATA_KEY,
    PRIORITY_METADATA_KEY,
    CapabilityMatcher,
    definition_enabled,
    definition_priority,
)
from .models import (
    DEFAULT_VERSION,
    CandidateMatch,
    Capability,
    CapabilityMatchStatus,
    CapabilityRef,
    CapabilityRequirement,
    CapabilityWorkMatch,
    MatchResult,
)
from .registry import CapabilityRegistry, as_refs, version_sort_key
from .trace import CapabilityTrace, get_capability_trace

__all__ = [
    "CandidateMatch",
    "Capability",
    "CapabilityMatchStatus",
    "CapabilityMatcher",
    "CapabilityRef",
    "CapabilityRegistry",
    "CapabilityRequirement",
    "CapabilityTrace",
    "CapabilityWorkMatch",
    "DEFAULT_VERSION",
    "ENABLED_METADATA_KEY",
    "MatchResult",
    "PRIORITY_METADATA_KEY",
    "as_refs",
    "definition_enabled",
    "definition_priority",
    "get_capability_trace",
    "version_sort_key",
]
