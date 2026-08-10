"""Process — the single unifying primitive of NEXUS SEED.

A **Process** is *anything* the system does.  Skill, Agent, Workflow, Harness,
Deep Research, a resident monitor — none of these are separate base types.  They
are all Processes; those words describe the *role* a process plays in a given
context, not a different data model (a role may be recorded in
``ProcessDefinition.metadata``, never as a new core type).

Two things are kept apart:

* :class:`ProcessDefinition` — *what* a process does (static, stateless).
* :class:`ProcessInstance` — a *running* process (has state, status, identity).

Every process handler shares the same execution interface::

    async def handler(ctx: ProcessContext) -> ProcessResult: ...

Handlers do not touch storage.  They stage state writes through ``ctx.state``
and return every effect in a :class:`ProcessResult`; the runtime commits the
whole thing in one transaction (Phase 2A: atomic process transition).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from .continuation import Continuation
from .context import Context
from .event import Event, utcnow
from .state import StateChange, StateView
from ..world.observation import Observation
from ..world.state_delta import StateDelta

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class ProcessStatus(str, Enum):
    """Lifecycle status of a :class:`ProcessInstance`."""

    RUNNABLE = "RUNNABLE"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RetryableError(Exception):
    """Raise from a handler to signal a *transient* failure worth retrying."""


@dataclass(frozen=True)
class ProcessDefinition:
    """The static definition of a process.

    Attributes:
        name: Logical name (e.g. ``"resistance_analysis"``).
        version: Definition version string.
        handler: Name of the registered handler callable to run.
        trigger_event_types: Event types that start a fresh instance.
        max_retries: How many times a retryable failure may be retried.
        metadata: Free-form tags (e.g. ``{"role": "skill"}``).  Never a new
            core type — just annotations on a Process.
    """

    name: str
    version: str
    handler: str
    trigger_event_types: tuple[str, ...] = ()
    max_retries: int = 0
    metadata: dict = field(default_factory=dict)


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
        retry_count: How many retries have been attempted.
        max_retries: Retry budget copied from the definition.
        next_retry_at: When a RETRY_WAIT instance becomes RUNNABLE again.
        last_error: Message from the most recent failure.
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
    retry_count: int = 0
    max_retries: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SpawnSpec:
    """A request to spawn a child process."""

    definition_name: str
    definition_version: str
    input: dict = field(default_factory=dict)
    priority: int = 0
    correlation_id: uuid.UUID | None = None


@dataclass
class TimerSpec:
    """A request to arm a timer that will emit a ``timer_fired`` event."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    delay: float | None = None
    fire_at: datetime | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class JoinRequest:
    """A request to suspend the parent until spawned children finish.

    Attributes:
        mode: ``"all"`` waits for every child; ``"any"`` for the first.
        resume_point: Where the parent resumes once the join is satisfied.
        saved_process_state: State handed back to the parent on resume.
    """

    mode: str
    resume_point: str
    saved_process_state: dict = field(default_factory=dict)


@dataclass
class ProcessResult:
    """The outcome of running a process handler — a batch of effects.

    The runtime applies all of these in a single transaction.

    Attributes:
        status: The status the instance should transition to.
        output: Optional structured output.
        state_changes: World-state writes to apply.
        emitted_events: New events the process produced.
        continuations_to_create: Continuations to persist (e.g. on suspend).
        continuations_to_delete: Continuation ids to remove.
        spawned_processes: Child processes to create.
        timers_to_create: Timers to arm.
        observations: Observations to persist.
        state_deltas: State deltas to persist.
        join: Optional join request (suspend until children finish).
        retryable: If FAILED, whether the failure may be retried.
        retry_delay: Optional explicit backoff (seconds) for a retry.
    """

    status: ProcessStatus
    output: dict | None = None
    state_changes: list[StateChange] = field(default_factory=list)
    emitted_events: list[Event] = field(default_factory=list)
    continuations_to_create: list[Continuation] = field(default_factory=list)
    continuations_to_delete: list[uuid.UUID] = field(default_factory=list)
    spawned_processes: list[SpawnSpec] = field(default_factory=list)
    timers_to_create: list[TimerSpec] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    state_deltas: list[StateDelta] = field(default_factory=list)
    join: JoinRequest | None = None
    retryable: bool = False
    retry_delay: float | None = None


@dataclass
class ProcessContext:
    """The handle passed to a process handler — its whole view of the world.

    A handler reads/writes world state through :attr:`state` (a
    :class:`~nexus_seed.core.state.StateView`; writes are staged), inspects the
    triggering/resuming :attr:`event`, and returns a :class:`ProcessResult`
    built with :meth:`complete`, :meth:`suspend`, :meth:`suspend_on_timer`,
    :meth:`spawn_and_join`, :meth:`retry` or :meth:`fail`.
    """

    instance: ProcessInstance
    event: Event | None
    state: StateView
    context: Context
    resume_point: str | None = None
    saved_process_state: dict = field(default_factory=dict)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("nexus_seed.process")
    )
    _observations: list[Observation] = field(default_factory=list)
    _state_deltas: list[StateDelta] = field(default_factory=list)

    @property
    def correlation_id(self) -> uuid.UUID | None:
        """Correlation id to propagate onto events this process emits."""
        if self.event is not None:
            return self.event.correlation_id or self.event.id
        cid = self.instance.input.get("correlation_id")
        return uuid.UUID(cid) if isinstance(cid, str) else None

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

    def observe(
        self,
        *,
        subject: str,
        predicate: str,
        extracted: dict,
        confidence: float = 1.0,
    ) -> Observation:
        """Record a semantic reading of the current event (staged on the result)."""
        observation = Observation(
            subject=subject,
            predicate=predicate,
            extracted=dict(extracted),
            source_event_id=self.event.id if self.event else None,
            created_by_process_id=self.instance.id,
            confidence=confidence,
        )
        self._observations.append(observation)
        return observation

    def propose_delta(
        self,
        *,
        entity: str,
        attribute: str,
        old_value: object,
        new_value: object,
        observation: Observation | None = None,
        confidence: float = 1.0,
        reason: str | None = None,
    ) -> StateDelta:
        """Propose a world-state change (staged on the result; not applied here)."""
        delta = StateDelta(
            entity=entity,
            attribute=attribute,
            old_value=old_value,
            new_value=new_value,
            source_event_id=self.event.id if self.event else None,
            observation_id=observation.id if observation else None,
            created_by_process_id=self.instance.id,
            confidence=confidence,
            reason=reason,
        )
        self._state_deltas.append(delta)
        return delta

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
            state_changes=list(self.state.changes),
            emitted_events=list(emitted_events or []),
            spawned_processes=list(spawned_processes or []),
            observations=list(self._observations),
            state_deltas=list(self._state_deltas),
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
            state_changes=list(self.state.changes),
            emitted_events=list(emitted_events or []),
            continuations_to_create=[continuation],
            observations=list(self._observations),
            state_deltas=list(self._state_deltas),
        )

    def suspend_on_timer(
        self,
        *,
        resume_point: str,
        delay: float | None = None,
        fire_at: datetime | None = None,
        saved_process_state: dict | None = None,
    ) -> ProcessResult:
        """Suspend until a timer fires (``delay`` seconds from now, or ``fire_at``)."""
        timer = TimerSpec(delay=delay, fire_at=fire_at)
        timer.payload = {"timer_id": str(timer.id)}
        continuation = Continuation(
            process_instance_id=self.instance.id,
            resume_point=resume_point,
            waiting_for={"event_type": "timer_fired", "timer_id": str(timer.id)},
            saved_process_state=dict(saved_process_state or {}),
        )
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            state_changes=list(self.state.changes),
            continuations_to_create=[continuation],
            timers_to_create=[timer],
        )

    def spawn_and_join(
        self,
        specs: list[SpawnSpec],
        *,
        mode: str,
        resume_point: str,
        saved_process_state: dict | None = None,
    ) -> ProcessResult:
        """Spawn ``specs`` as children and suspend until ``mode`` are done."""
        if mode not in ("all", "any"):
            raise ValueError(f"join mode must be 'all' or 'any', got {mode!r}")
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            state_changes=list(self.state.changes),
            spawned_processes=list(specs),
            join=JoinRequest(
                mode=mode,
                resume_point=resume_point,
                saved_process_state=dict(saved_process_state or {}),
            ),
        )

    def retry(self, error: object, *, delay: float | None = None) -> ProcessResult:
        """Return a *retryable* failure result (no side effects are applied)."""
        return ProcessResult(
            status=ProcessStatus.FAILED,
            output={"error": str(error)},
            retryable=True,
            retry_delay=delay,
        )

    def fail(
        self,
        error: object,
        *,
        emitted_events: list[Event] | None = None,
    ) -> ProcessResult:
        """Return a non-retryable FAILED result."""
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
