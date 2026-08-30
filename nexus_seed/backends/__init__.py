"""Swappable structured reasoning backends."""

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
