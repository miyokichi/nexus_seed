"""Concrete process definitions and handlers (all ordinary Processes)."""

from .demo_resistance import DEFINITION as RESISTANCE_ANALYSIS
from .demo_resistance import bootstrap, resistance_analysis
from .semantic import (
    APPLY as APPLY_STATE_DELTA,
)
from .semantic import (
    INTERPRET as INTERPRET_EVENT,
)
from .semantic import apply_state_delta, bootstrap_semantic, interpret_event
from .work_intelligence import (
    bootstrap_work_intelligence,
    impact_analysis,
    missing_work_detector,
    resistance_check,
    work_matcher,
    work_spawner,
)
from .llm_interpret import (
    INTERPRET_LLM,
    bootstrap_llm_interpreter,
    interpret_event_llm,
)
from .extension import (
    ANALYZE_CAPABILITY_GAP,
    RECONCILE_CAPABILITY_GAPS,
    analyze_capability_gap,
    bootstrap_extension,
    reconcile_capability_gaps,
)
from .construction import (
    CLEANUP_EXTENSION_LIFECYCLE,
    EXECUTE_EXTENSION_CONSTRUCTION,
    PLAN_EXTENSION_CONSTRUCTION,
    VERIFY_EXTENSION_CONSTRUCTION,
    bootstrap_construction,
    cleanup_extension_lifecycle,
    execute_extension_construction,
    plan_extension_construction,
    verify_extension_construction,
)
from .installation import (
    ACTIVATE_INSTALLED_EXTENSION,
    INSTALL_EXTENSION,
    PLAN_EXTENSION_INSTALLATION,
    ROLLBACK_INSTALLATION,
    VERIFY_INSTALLED_EXTENSION,
    bootstrap_installation,
)
from .autonomy import (
    ADVANCE_CAPABILITY_ACQUISITION,
    advance_capability_acquisition,
    bootstrap_autonomy,
)

__all__ = [
    "RESISTANCE_ANALYSIS",
    "bootstrap",
    "resistance_analysis",
    "INTERPRET_EVENT",
    "APPLY_STATE_DELTA",
    "interpret_event",
    "apply_state_delta",
    "bootstrap_semantic",
    "impact_analysis",
    "work_matcher",
    "missing_work_detector",
    "work_spawner",
    "resistance_check",
    "bootstrap_work_intelligence",
    "INTERPRET_LLM",
    "interpret_event_llm",
    "bootstrap_llm_interpreter",
    "ANALYZE_CAPABILITY_GAP",
    "RECONCILE_CAPABILITY_GAPS",
    "analyze_capability_gap",
    "reconcile_capability_gaps",
    "bootstrap_extension",
    "PLAN_EXTENSION_CONSTRUCTION",
    "EXECUTE_EXTENSION_CONSTRUCTION",
    "VERIFY_EXTENSION_CONSTRUCTION",
    "CLEANUP_EXTENSION_LIFECYCLE",
    "plan_extension_construction",
    "execute_extension_construction",
    "verify_extension_construction",
    "cleanup_extension_lifecycle",
    "bootstrap_construction",
    "PLAN_EXTENSION_INSTALLATION",
    "INSTALL_EXTENSION",
    "VERIFY_INSTALLED_EXTENSION",
    "ACTIVATE_INSTALLED_EXTENSION",
    "ROLLBACK_INSTALLATION",
    "bootstrap_installation",
    "ADVANCE_CAPABILITY_ACQUISITION",
    "advance_capability_acquisition",
    "bootstrap_autonomy",
]
