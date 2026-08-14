"""Shared scaffolding for the Phase 4B.1 bounded-drain tests (not a test module).

Everything here exists to make one question askable: *what happens when a
runtime call is not allowed to run the system to quiescence?*  So the processes
are deliberately event-productive — each activation makes more work — which is
exactly the shape that used to make ``submit_event`` unbounded.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.drain import UNLIMITED, DrainBudget, DrainResult
from nexus_seed.runtime.runtime import Runtime

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Every activation, in order, as ``{"definition": ..., "n": ...}``.
RECORDED: list = []


def drain_runtime(tmp_path, name="drain.db"):
    """A runtime with a manual clock; nothing else is registered yet."""
    RECORDED.clear()
    clock = ManualClock(EPOCH)
    return Runtime(tmp_path / name, clock=clock), clock


def step(n: int = 0, *, event_type: str = "step") -> Event:
    """One link of a chain."""
    return Event(event_type, "test", {"n": n})


def _definition(name: str, trigger: str) -> ProcessDefinition:
    return ProcessDefinition(
        name=name,
        version="1",
        handler=name,
        trigger_event_types=(trigger,),
        context_requirements=ContextRequirements(include_trigger_event=True),
    )


def register_chain(runtime, *, length: int = 5, name: str = "stepper"):
    """A process that continues itself ``length`` times, then stops.

    A finite amount of work that arrives a bit at a time — the honest case for
    a budget, where yielding early must lose nothing.
    """

    async def stepper(ctx):
        n = int(ctx.event.payload.get("n", 0))
        RECORDED.append({"definition": name, "n": n})
        if n + 1 >= length:
            return ctx.complete(output={"n": n, "last": True})
        return ctx.complete(
            output={"n": n},
            emitted_events=[ctx.new_event("step", {"n": n + 1})],
        )

    runtime.register_process(_definition(name, "step"), stepper)
    return runtime


def register_endless(runtime, *, name: str = "forever"):
    """A process that re-triggers itself for ever.

    Without a budget this is a hang: the drain loop would never find the world
    quiet.  It is here to prove a budget is a real bound, not a hint (spec §48)
    — so never call an unbudgeted drain on a runtime wired with this.
    """

    async def forever(ctx):
        n = int(ctx.event.payload.get("n", 0))
        RECORDED.append({"definition": name, "n": n})
        return ctx.complete(
            output={"n": n},
            emitted_events=[ctx.new_event("step", {"n": n + 1})],
        )

    runtime.register_process(_definition(name, "step"), forever)
    return runtime


def register_fanout(runtime, *, width: int = 3, name: str = "fanout"):
    """A process whose single activation creates ``width`` more events."""

    async def fanout(ctx):
        n = int(ctx.event.payload.get("n", 0))
        RECORDED.append({"definition": name, "n": n})
        if n > 0:
            return ctx.complete(output={"n": n})
        return ctx.complete(
            output={"n": n},
            emitted_events=[
                ctx.new_event("step", {"n": i + 1}) for i in range(width)
            ],
        )

    runtime.register_process(_definition(name, "step"), fanout)
    return runtime


def pending(runtime) -> int:
    """Deliveries still owed."""
    return runtime.get_pending_event_delivery_count()


def activations() -> int:
    """How many times any registered handler has run."""
    return len(RECORDED)


__all__ = [
    "EPOCH",
    "RECORDED",
    "UNLIMITED",
    "DrainBudget",
    "DrainResult",
    "activations",
    "drain_runtime",
    "pending",
    "register_chain",
    "register_endless",
    "register_fanout",
    "step",
]
