"""Execution backends: swappable reasoning engines callable from Processes."""

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
]
