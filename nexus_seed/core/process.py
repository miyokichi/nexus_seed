"""Process — the single executable primitive of NEXUS SEED.

``ProcessDefinition`` describes static code and ``ProcessInstance`` is its
durable running copy. Handlers stage effects in ``ProcessResult``; the Runtime
commits the whole activation atomically.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from ..context.requirements import ContextRequirements
from .continuation import Continuation
from .context import Context
from .event import Event, utcnow
from .state import StateChange, StateView

if TYPE_CHECKING:  # pragma: no cover
    from ..context.models import ProcessContextView
    from ..resources.models import Resource, ResourceRepresentation, ResourceVersion


class ProcessStatus(str, Enum):
    """Lifecycle status of a ProcessInstance."""

    RUNNABLE = "RUNNABLE"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"  # retained so databases created before cleanup load


class RetryableError(Exception):
    """Signal a transient handler failure worth retrying."""


@dataclass(frozen=True)
class ProcessDefinition:
    """Static definition of one Process implementation."""

    name: str
    version: str
    handler: str
    trigger_event_types: tuple[str, ...] = ()
    max_retries: int = 0
    metadata: dict = field(default_factory=dict)
    context_requirements: ContextRequirements | None = None


@dataclass
class ProcessInstance:
    """Durable running copy of a ProcessDefinition."""

    definition_name: str
    definition_version: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: ProcessStatus = ProcessStatus.RUNNABLE
    input: dict = field(default_factory=dict)
    local_state: dict = field(default_factory=dict)
    parent_process_id: uuid.UUID | None = None
    priority: int = 0
    pending_event_id: uuid.UUID | None = None
    trigger_event_id: uuid.UUID | None = None
    retry_count: int = 0
    max_retries: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SpawnSpec:
    """Request to create one child ProcessInstance."""

    definition_name: str
    definition_version: str
    input: dict = field(default_factory=dict)
    priority: int = 0
    correlation_id: uuid.UUID | None = None


@dataclass
class TimerSpec:
    """Request to arm a durable timer."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    delay: float | None = None
    fire_at: datetime | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class JoinRequest:
    """Request to suspend a parent until spawned children finish."""

    mode: str
    resume_point: str
    saved_process_state: dict = field(default_factory=dict)


@dataclass
class ProcessResult:
    """Atomic outcome of one Process activation."""

    status: ProcessStatus
    output: dict | None = None
    state_changes: list[StateChange] = field(default_factory=list)
    emitted_events: list[Event] = field(default_factory=list)
    continuations_to_create: list[Continuation] = field(default_factory=list)
    continuations_to_delete: list[uuid.UUID] = field(default_factory=list)
    spawned_processes: list[SpawnSpec] = field(default_factory=list)
    timers_to_create: list[TimerSpec] = field(default_factory=list)
    resources: list["Resource"] = field(default_factory=list)
    resource_versions: list["ResourceVersion"] = field(default_factory=list)
    resource_representations: list["ResourceRepresentation"] = field(default_factory=list)
    join: JoinRequest | None = None
    retryable: bool = False
    retry_delay: float | None = None


@dataclass
class ProcessContext:
    """The complete handle passed to a Process handler."""

    instance: ProcessInstance
    event: Event | None
    state: StateView
    view: "ProcessContextView | None" = None
    context: Context | None = None
    resume_point: str | None = None
    saved_process_state: dict = field(default_factory=dict)
    services: object | None = None
    backends: dict = field(default_factory=dict)
    adapters: object | None = None
    ingress: object | None = None
    project_orchestrator: object | None = None
    context_snapshot_id: uuid.UUID | None = None
    activation_id: str | None = None
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("nexus_seed.process")
    )
    _resources: list["Resource"] = field(default_factory=list)
    _resource_versions: list["ResourceVersion"] = field(default_factory=list)
    _resource_representations: list["ResourceRepresentation"] = field(
        default_factory=list
    )

    @property
    def correlation_id(self) -> uuid.UUID | None:
        """Correlation id inherited by an emitted Event."""
        if self.event is not None:
            return self.event.correlation_id or self.event.id
        value = self.instance.input.get("correlation_id")
        try:
            return uuid.UUID(value) if isinstance(value, str) else value
        except ValueError:
            return None

    def new_event(
        self, event_type: str, payload: dict, *, source: str | None = None
    ) -> Event:
        """Build a causally linked Event emitted by this activation."""
        return Event(
            type=event_type,
            source=source or f"process:{self.instance.definition_name}",
            payload=dict(payload),
            correlation_id=self.correlation_id,
            causation_id=self.event.id if self.event is not None else None,
        )

    def add_resource(self, resource: "Resource") -> "Resource":
        """Stage a Resource write."""
        self._resources.append(resource)
        return resource

    def add_resource_version(self, version: "ResourceVersion") -> "ResourceVersion":
        """Stage an immutable ResourceVersion write."""
        self._resource_versions.append(version)
        return version

    def add_representation(
        self, representation: "ResourceRepresentation"
    ) -> "ResourceRepresentation":
        """Stage a ResourceRepresentation write."""
        if representation.created_by_process_id is None:
            representation.created_by_process_id = self.instance.id
        self._resource_representations.append(representation)
        return representation

    def _staged(self) -> dict:
        return {
            "resources": list(self._resources),
            "resource_versions": list(self._resource_versions),
            "resource_representations": list(self._resource_representations),
        }

    def complete(
        self,
        output: dict | None = None,
        *,
        emitted_events: list[Event] | None = None,
        spawned_processes: list[SpawnSpec] | None = None,
    ) -> ProcessResult:
        """Complete this activation with its staged effects."""
        return ProcessResult(
            status=ProcessStatus.COMPLETED,
            output=output,
            state_changes=list(self.state.changes),
            emitted_events=list(emitted_events or []),
            spawned_processes=list(spawned_processes or []),
            **self._staged(),
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
        """Suspend on a logical Continuation."""
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
            **self._staged(),
        )

    def suspend_on_timer(
        self,
        *,
        resume_point: str,
        delay: float | None = None,
        fire_at: datetime | None = None,
        saved_process_state: dict | None = None,
        also_waiting_for: list[dict] | None = None,
        emitted_events: list[Event] | None = None,
    ) -> ProcessResult:
        """Suspend until a durable timer or an alternative Event fires."""
        timer = TimerSpec(delay=delay, fire_at=fire_at)
        timer.payload = {"timer_id": str(timer.id)}
        on_timer = {"event_type": "timer_fired", "timer_id": str(timer.id)}
        waiting_for = (
            {"any": [on_timer, *also_waiting_for]}
            if also_waiting_for
            else on_timer
        )
        continuation = Continuation(
            process_instance_id=self.instance.id,
            resume_point=resume_point,
            waiting_for=waiting_for,
            saved_process_state=dict(saved_process_state or {}),
        )
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            state_changes=list(self.state.changes),
            emitted_events=list(emitted_events or []),
            continuations_to_create=[continuation],
            timers_to_create=[timer],
            **self._staged(),
        )

    def spawn_and_join(
        self,
        specs: list[SpawnSpec],
        *,
        mode: str,
        resume_point: str,
        saved_process_state: dict | None = None,
    ) -> ProcessResult:
        """Spawn children and suspend until all or any finish."""
        if mode not in {"all", "any"}:
            raise ValueError(f"join mode must be 'all' or 'any', got {mode!r}")
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            state_changes=list(self.state.changes),
            spawned_processes=list(specs),
            join=JoinRequest(mode, resume_point, dict(saved_process_state or {})),
            **self._staged(),
        )

    def retry(self, error: object, *, delay: float | None = None) -> ProcessResult:
        """Return a retryable failure without applying staged writes."""
        return ProcessResult(
            status=ProcessStatus.FAILED,
            output={"error": str(error)},
            retryable=True,
            retry_delay=delay,
        )

    def fail(
        self, error: object, *, emitted_events: list[Event] | None = None
    ) -> ProcessResult:
        """Return a terminal failure."""
        return ProcessResult(
            status=ProcessStatus.FAILED,
            output={"error": str(error)},
            emitted_events=list(emitted_events or []),
        )


Handler = Callable[[ProcessContext], Awaitable[ProcessResult]]


class HandlerRegistry:
    """In-memory mapping from persisted handler names to callables."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        """Register or replace a handler."""
        self._handlers[name] = handler

    def get(self, name: str) -> Handler:
        """Return a handler, raising a useful error when it is unavailable."""
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise KeyError(f"handler {name!r} is not registered") from exc

    def __contains__(self, name: object) -> bool:
        return name in self._handlers
