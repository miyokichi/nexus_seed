"""Shared scaffolding for the Phase 3F delivery tests (not a test module)."""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.delivery.models import EventDeliveryStatus
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

RECORDED: list = []


def manual_runtime(tmp_path, name="d.db"):
    """A runtime with a manual clock, for deterministic backoff tests."""
    clock = ManualClock(EPOCH)
    return Runtime(tmp_path / name, clock=clock), clock


def noted_definition(
    name: str = "noter", *, trigger: str = "ping", version: str = "1"
) -> ProcessDefinition:
    """A definition whose handler just records that it ran."""
    return ProcessDefinition(
        name=name,
        version=version,
        handler=name,
        trigger_event_types=(trigger,),
        context_requirements=ContextRequirements(include_trigger_event=True),
    )


async def noter(ctx):
    """Record the activation and complete."""
    RECORDED.append(
        {
            "definition": ctx.instance.definition_name,
            "event_id": str(ctx.event.id) if ctx.event else None,
            "instance_id": str(ctx.instance.id),
        }
    )
    return ctx.complete(output={"noted": True})


def register_noter(runtime, name="noter", *, trigger="ping"):
    """Register a recording process and clear the recording buffer."""
    RECORDED.clear()
    runtime.register_process(noted_definition(name, trigger=trigger), noter)
    return runtime


def ping(payload=None) -> Event:
    """The trigger event the recording process listens for."""
    return Event("ping", "test", payload or {})


def status_of(runtime, event_id) -> str:
    """The delivery status of an event, as a plain string."""
    delivery = runtime.get_event_delivery(event_id)
    return delivery.status.value if delivery else "MISSING"


def instances_named(runtime, name) -> list:
    """Return every instance of the definition ``name``, oldest first."""
    return [i for i in runtime.process_store.all_instances() if i.definition_name == name]


def outstanding(runtime) -> list:
    """Every delivery that still owes a routing attempt."""
    return [d for d in runtime.get_event_deliveries() if d.outstanding]


class BrokenRouter:
    """A router that fails a fixed number of times, then delegates.

    Used to prove that a routing failure is transient bookkeeping, not lost
    work: the event stays outstanding and the next sweep retries it.
    """

    def __init__(self, real, failures: int = 1, *, only_type: str | None = None) -> None:
        self.real = real
        self.remaining = failures
        self.only_type = only_type
        self.attempts = 0

    def route(self, event):
        self.attempts += 1
        if self.remaining > 0 and (self.only_type is None or event.type == self.only_type):
            self.remaining -= 1
            raise RuntimeError(f"router exploded on {event.type}")
        return self.real.route(event)


def break_router(runtime, failures: int = 1, *, only_type: str | None = None) -> BrokenRouter:
    """Install a failing router on ``runtime``'s dispatcher."""
    broken = BrokenRouter(runtime.router, failures, only_type=only_type)
    runtime.dispatcher.router = broken
    return broken


def heal_router(runtime) -> None:
    """Put the real router back."""
    runtime.dispatcher.router = runtime.router


__all__ = [
    "BrokenRouter",
    "EPOCH",
    "EventDeliveryStatus",
    "RECORDED",
    "break_router",
    "heal_router",
    "instances_named",
    "manual_runtime",
    "noted_definition",
    "noter",
    "outstanding",
    "ping",
    "register_noter",
    "status_of",
]
