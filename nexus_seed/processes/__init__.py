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
]
