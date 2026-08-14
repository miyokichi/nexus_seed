"""Bounded drain — one runtime call does a slice of work, not all of it.

Until now a drain ran until the world stood still.  That was fine when chains
were short, and it stops being fine as soon as a plan is long or a process can
cause its own next event: one ``submit_event`` could block for an unbounded
time, and a self-triggering loop would never return at all.

Phase 4B.1 says plainly that **one call need not reach quiescence**
(Invariant 72).  A budget stops the slice; everything is already durable, so
the next tick continues (Invariant 73).

Reaching a budget is **not a failure** (Invariant 71).  Nothing is marked
FAILED, no event is dropped, no plan is abandoned — the runtime simply stops
here and says so.

A budget is a parameter of a call, not stored state (spec §102): the truth of
what remains already lives in ``event_deliveries``, ``process_instances``,
``continuations``, ``plan_nodes`` and ``timers``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DrainBudget:
    """How much work one drain call may do before yielding.

    ``None`` on a field means "no limit for this dimension", and the default
    budget is unlimited — existing callers keep the old behaviour exactly
    (spec §42).

    Attributes:
        max_dispatches: Event deliveries to route.
        max_activations: Process activations to run.
        max_cycles: Iterations of the dispatch/execute loop, as a backstop for
            work that produces neither (nothing does today, but a budget that
            cannot expire is not a budget).
    """

    max_dispatches: int | None = None
    max_activations: int | None = None
    max_cycles: int | None = None

    @property
    def unlimited(self) -> bool:
        """Whether this budget imposes no limit at all."""
        return (
            self.max_dispatches is None
            and self.max_activations is None
            and self.max_cycles is None
        )

    def dispatch_allowance(self, used: int) -> int | None:
        """How many more dispatches are permitted (``None`` = unlimited)."""
        if self.max_dispatches is None:
            return None
        return max(self.max_dispatches - used, 0)

    def exhausted_by(self, *, dispatches: int, activations: int, cycles: int) -> bool:
        """Whether this slice has reached any of its limits."""
        return (
            (self.max_dispatches is not None and dispatches >= self.max_dispatches)
            or (self.max_activations is not None and activations >= self.max_activations)
            or (self.max_cycles is not None and cycles >= self.max_cycles)
        )


#: The default: keep going until the world is quiet, as before Phase 4B.1.
UNLIMITED = DrainBudget()


@dataclass
class DrainResult:
    """What one drain slice did, and what it left behind.

    ``exhausted`` says the slice stopped at its budget rather than because
    there was nothing left — the caller's cue to come back, not to worry.
    """

    dispatches: int = 0
    activations: int = 0
    cycles: int = 0
    exhausted: bool = False
    remaining_deliveries: int = 0
    remaining_runnable_processes: int = 0
    produced_events: list = field(default_factory=list)

    @property
    def idle(self) -> bool:
        """Whether the runtime genuinely ran out of work."""
        return not self.exhausted and not self.has_remaining

    @property
    def has_remaining(self) -> bool:
        """Whether durable work is still waiting."""
        return bool(self.remaining_deliveries or self.remaining_runnable_processes)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"DrainResult(dispatches={self.dispatches}, "
            f"activations={self.activations}, exhausted={self.exhausted}, "
            f"remaining={self.remaining_deliveries}/"
            f"{self.remaining_runnable_processes})"
        )
