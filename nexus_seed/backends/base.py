"""ExecutionBackend — a swappable reasoning engine a Process can call.

A backend turns a request into a raw result.  That is *all* it does.  It must
never touch world state, observations, deltas, work, spawns, continuations or
policy (Invariant 19) — those are the Process's job, downstream of the backend.

The same interface is used by the real LLM backend and the fake one used in
tests (Invariant 20), so nothing about a particular provider leaks into process
semantics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..core.event import utcnow


@dataclass
class BackendRequest:
    """Input to a backend call.

    Attributes:
        instruction: What the backend should do.
        context: The compiled context to reason over (a serialized
            ProcessContextView or plain dict) — never a raw store query.
        output_schema: The structure the backend must return, if any.
        metadata: Extra call metadata (trigger type, text, …).
    """

    instruction: str
    context: dict = field(default_factory=dict)
    output_schema: Any = None
    metadata: dict = field(default_factory=dict)


@dataclass
class BackendResult:
    """Output of a backend call (raw — not yet a Proposal)."""

    raw_output: Any = None
    parsed_output: Any = None
    model: str | None = None
    usage: dict | None = None
    latency_ms: float | None = None
    success: bool = True
    error: str | None = None


@dataclass
class LLMInvocation:
    """An audit record of a single backend call (no secrets required)."""

    process_instance_id: uuid.UUID
    backend: str
    activation_id: str | None = None
    model: str | None = None
    request_metadata: dict = field(default_factory=dict)
    response_metadata: dict = field(default_factory=dict)
    context_snapshot_id: uuid.UUID | None = None
    success: bool = True
    error: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


@runtime_checkable
class ExecutionBackend(Protocol):
    """A reasoning engine that maps a :class:`BackendRequest` to a result."""

    async def execute(self, request: BackendRequest) -> BackendResult:
        """Run the request and return a raw result (never mutating state)."""
        ...
