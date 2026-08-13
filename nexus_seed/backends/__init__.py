"""Execution backends: swappable engines a Process can call.

Two families, one shape.  A *reasoning* backend (:class:`LLMBackend`) turns a
request into structured output; an *action* backend
(:class:`LocalFileActionBackend`) turns a request into an external effect.
Neither judges, decides or writes state — that stays in Processes.
"""

from .base import (
    BackendRequest,
    BackendResult,
    ExecutionBackend,
    LLMInvocation,
)
from .llm import (
    FakeLLMBackend,
    LLMBackend,
    failure_response,
    invalid_response,
    proposal_response,
)
from .action import (
    ActionBackend,
    ActionCapability,
    ActionRequest,
    ActionResult,
    BackendCapabilities,
    FakeActionBackend,
    LocalFileActionBackend,
    action_failure,
    action_success,
    action_timeout,
    capabilities_of,
)

__all__ = [
    "BackendRequest",
    "BackendResult",
    "ExecutionBackend",
    "LLMInvocation",
    "FakeLLMBackend",
    "LLMBackend",
    "failure_response",
    "invalid_response",
    "proposal_response",
    "ActionBackend",
    "ActionCapability",
    "ActionRequest",
    "ActionResult",
    "BackendCapabilities",
    "FakeActionBackend",
    "LocalFileActionBackend",
    "action_failure",
    "action_success",
    "action_timeout",
    "capabilities_of",
]
