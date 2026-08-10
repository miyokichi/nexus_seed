"""Concrete process definitions and handlers (all ordinary Processes)."""

from .demo_resistance import DEFINITION as RESISTANCE_ANALYSIS
from .demo_resistance import bootstrap, resistance_analysis

__all__ = ["RESISTANCE_ANALYSIS", "bootstrap", "resistance_analysis"]
