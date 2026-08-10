"""Process — the single unifying primitive of NEXUS SEED.

A **Process** is *anything* the system does.  Skill, Agent, Workflow, Harness,
Deep Research, a resident monitor — none of these are separate base types.  They
are all Processes; those words describe the *role* a process plays in a given
context, not a different data model.

Two things are kept apart:

* :class:`ProcessDefinition` — *what* a process does (static, stateless).
* :class:`ProcessInstance` — a *running* process (has state, status, identity).

Many instances can be created from one definition.

Every process handler shares the same execution interface::

    async def handler(ctx: ProcessContext) -> ProcessResult: ...
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from .continuation import Continuation
from .context import Context
from .event import Event, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..storage.state_store import StateStore


class ProcessStatus(str, Enum):
    """Lifecycle status of a :class:`ProcessInstance`."""

    RUNNABLE = "RUNNABLE"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ProcessDefinition:
    """The static definition of a process.

    Attributes:
        name: Logical name (e.g. ``"resistance_analysis"``).
        version: Definition version string.
        handler: Name of the registered handler callable to run.
        trigger_event_types: Event types that start a fresh instance of this
            definition.  Kept simple in Phase 1 (a plain tuple of type names);
            the field is the extension point for richer triggering later.
    """

    name: str
    version: str
    handler: str
    trigger_event_types: tuple[str, ...] = ()


@dataclass
class ProcessInstance:
    """A concrete, running (or paused/finished) process.

    Attributes:
        definition_name: Name of the :class:`ProcessDefinition`.
        definition_version: Version of the definition.
        id: Unique identifier.
        status: Current :class:`ProcessStatus`.
        input: The input the instance was created with.
        local_state: Mutable per-instance working state.
        parent_process_id: The spawning instance, if any.
        priority: Higher runs first when several are RUNNABLE.
        pending_event_id: Runtime coordination field — the event that will
            (re)activate this instance on its next run.  Persisted so activation
            survives a runtime restart.
        created_at: When the instance was created (UTC).
        updated_at: When the instance was last updated (UTC).
    """

    definition_name: str
    definition_version: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: ProcessStatus = ProcessStatus.RUNNABLE
    input: dict = field(default_factory=dict)
    local_state: dict = field(default_factory=dict)
    parent_process_id: uuid.UUID | None = None
    priority: int = 0
    pending_event_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SpawnSpec:
    """A request to spawn a child process.

    Phase 1 keeps spawning minimal but the structure exists so it can grow.
    """

    definition_name: str
    definition_version: str
    input: dict = field(default_factory=dict)
    priority: int = 0


@dataclass
class ProcessResult:
    """The outcome of running a process handler.

    Attributes:
        status: The status the instance should transition to.
        output: Optional structured output.
        emitted_events: New events the process produced.
        spawned_processes: Child processes to create.
        continuation: Set when the process suspended; describes how to resume.
    """

    status: ProcessStatus
    output: dict | None = None
    emitted_events: list[Event] = field(default_factory=list)
    spawned_processes: list[SpawnSpec] = field(default_factory=list)
    continuation: Continuation | None = None


@dataclass
class ProcessContext:
    """The handle passed to a process handler — its whole view of the world.

    A handler reads/writes world state through :attr:`state`, inspects the
    triggering/resuming :attr:`event`, and returns a :class:`ProcessResult`
    built with the :meth:`complete`, :meth:`suspend` or :meth:`fail` helpers.
    """

    instance: ProcessInstance
    event: Event | None
    state: "StateStore"
    context: Context
    resume_point: str | None = None
    saved_process_state: dict = field(default_factory=dict)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("nexus_seed.process")
    )

    @property
    def correlation_id(self) -> uuid.UUID | None:
        """Correlation id to propagate onto events this process emits."""
        if self.event is None:
            return None
        return self.event.correlation_id or self.event.id

    def new_event(
        self,
        type: str,
        payload: dict | None = None,
        *,
        source: str | None = None,
    ) -> Event:
        """Create an event caused by the current activation.

        The new event inherits this activation's ``correlation_id`` and records
        the triggering event as its ``causation_id`` so causal chains are kept.
        """
        return Event(
            type=type,
            source=source or self.instance.definition_name,
            payload=payload or {},
            correlation_id=self.correlation_id,
            causation_id=self.event.id if self.event else None,
        )

    def complete(
        self,
        output: dict | None = None,
        *,
        emitted_events: list[Event] | None = None,
        spawned_processes: list[SpawnSpec] | None = None,
    ) -> ProcessResult:
        """Return a result marking the process COMPLETED."""
        return ProcessResult(
            status=ProcessStatus.COMPLETED,
            output=output,
            emitted_events=list(emitted_events or []),
            spawned_processes=list(spawned_processes or []),
        )

    def suspend(
        self,
        *,
        resume_point: str,
        waiting_for: dict,
        saved_process_state: dict | None = None,
        context_ref: str | None = None,
        emitted_events: list[Event] | None = None,
    ) -> ProcessResult:
        """Return a result marking the process SUSPENDED with a continuation."""
        continuation = Continuation(
            process_instance_id=self.instance.id,
            resume_point=resume_point,
            waiting_for=dict(waiting_for),
            saved_process_state=dict(saved_process_state or {}),
            context_ref=context_ref,
        )
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            emitted_events=list(emitted_events or []),
            continuation=continuation,
        )

    def fail(
        self,
        error: object,
        *,
        emitted_events: list[Event] | None = None,
    ) -> ProcessResult:
        """Return a result marking the process FAILED."""
        return ProcessResult(
            status=ProcessStatus.FAILED,
            output={"error": str(error)},
            emitted_events=list(emitted_events or []),
        )


# A process handler: async callable taking a ProcessContext, returning a result.
Handler = Callable[[ProcessContext], Awaitable[ProcessResult]]


class HandlerRegistry:
    """Maps handler names (as stored on definitions) to callables.

    Handlers are code, not data, so they are re-registered on every runtime
    construction.  Definitions in SQLite reference a handler by name.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        """Register ``handler`` under ``name`` (overwrites any existing)."""
        self._handlers[name] = handler

    def get(self, name: str) -> Handler:
        """Return the handler registered under ``name``.

        Raises:
            KeyError: If no handler is registered under that name.
        """
        try:
            return self._handlers[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"no handler registered for {name!r}") from exc

    def __contains__(self, name: object) -> bool:
        return name in self._handlers
