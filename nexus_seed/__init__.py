"""NEXUS SEED — an event-driven runtime built on a single Process primitive.

Phase 1 implements the minimal loop::

    Event -> Process -> State -> Continuation -> Event -> Resume

No LLM/AI features are included; this package is the mechanism they will later
plug into.
"""

from .core import (
    Continuation,
    Context,
    Event,
    HandlerRegistry,
    ProcessContext,
    ProcessDefinition,
    ProcessInstance,
    ProcessResult,
    ProcessStatus,
    StateEntry,
)
from .runtime import Runtime

__version__ = "0.1.0"

__all__ = [
    "Continuation",
    "Context",
    "Event",
    "HandlerRegistry",
    "ProcessContext",
    "ProcessDefinition",
    "ProcessInstance",
    "ProcessResult",
    "ProcessStatus",
    "StateEntry",
    "Runtime",
    "__version__",
]
