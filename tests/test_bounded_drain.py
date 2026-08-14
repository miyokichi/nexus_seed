"""AT34–AT41 (spec §41–§48): one call does a slice of work, not all of it.

Until Phase 4B.1 a drain ran until the world stood still.  That was fine while
chains were short and stops being fine the moment a plan is long or a process
can cause its own next event: ``submit_event`` could block for an unbounded
time, and a self-triggering loop would never return at all.

The rule that makes a budget safe rather than lossy: **reaching one is not a
failure** (Invariant 71).  Nothing is marked FAILED, nothing is dropped —
everything is already durable, so the next call continues (Invariant 73).
"""

from __future__ import annotations

from drain_helpers import (
    RECORDED,
    UNLIMITED,
    DrainBudget,
    drain_runtime,
    pending,
    register_chain,
    step,
)


# --- the budget itself -----------------------------------------------------


def test_the_default_budget_is_unlimited():
    """Spec §42: every existing caller keeps the old behaviour exactly."""
    assert UNLIMITED.unlimited
    assert DrainBudget().unlimited
    assert not DrainBudget(max_activations=1).unlimited


def test_an_unlimited_budget_is_never_exhausted():
    assert not UNLIMITED.exhausted_by(dispatches=10**6, activations=10**6, cycles=10**6)
    assert UNLIMITED.dispatch_allowance(500) is None


def test_each_dimension_can_stop_a_slice_on_its_own():
    assert DrainBudget(max_dispatches=2).exhausted_by(dispatches=2, activations=0, cycles=0)
    assert DrainBudget(max_activations=2).exhausted_by(dispatches=0, activations=2, cycles=0)
    assert DrainBudget(max_cycles=2).exhausted_by(dispatches=0, activations=0, cycles=2)
    assert not DrainBudget(max_activations=2).exhausted_by(
        dispatches=99, activations=1, cycles=99
    )


def test_the_dispatch_allowance_never_goes_negative():
    budget = DrainBudget(max_dispatches=3)
    assert budget.dispatch_allowance(0) == 3
    assert budget.dispatch_allowance(3) == 0
    assert budget.dispatch_allowance(5) == 0


# --- draining a slice ------------------------------------------------------


async def test_a_budget_stops_the_slice_early(tmp_path):
    """AT34."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=5)

    await runtime.submit_event(step(0), DrainBudget(max_activations=2))

    assert len(RECORDED) == 2
    assert runtime.last_drain.exhausted
    assert runtime.last_drain.activations == 2
    runtime.close()


async def test_yielding_at_a_budget_is_not_a_failure(tmp_path):
    """AT35: the central claim (Invariant 71)."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=5)

    await runtime.submit_event(step(0), DrainBudget(max_activations=2))

    from nexus_seed.core.process import ProcessStatus

    assert runtime.process_store.instances_by_status(ProcessStatus.FAILED) == []
    assert runtime.last_drain.has_remaining
    assert not runtime.last_drain.idle
    runtime.close()


async def test_the_next_call_continues_where_the_last_one_stopped(tmp_path):
    """AT36: durable yield (Invariant 73)."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=5)

    await runtime.submit_event(step(0), DrainBudget(max_activations=2))
    seen = len(RECORDED)
    while runtime.last_drain.has_remaining:
        await runtime.drain(DrainBudget(max_activations=2))
        assert len(RECORDED) > seen
        seen = len(RECORDED)

    assert [r["n"] for r in RECORDED] == [0, 1, 2, 3, 4]
    assert pending(runtime) == 0
    runtime.close()


async def test_an_unbudgeted_drain_still_runs_to_quiescence(tmp_path):
    """AT37: nothing about the old contract changed for callers who want it."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=5)

    await runtime.submit_event(step(0))

    assert [r["n"] for r in RECORDED] == [0, 1, 2, 3, 4]
    assert runtime.last_drain.idle
    assert not runtime.last_drain.exhausted
    runtime.close()


async def test_the_result_reports_what_the_slice_did_and_what_is_left(tmp_path):
    """AT38: a caller must be able to tell 'stopped early' from 'finished'."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=4)

    await runtime.submit_event(step(0), DrainBudget(max_activations=1))
    stopped = runtime.last_drain
    assert stopped.exhausted and stopped.has_remaining
    assert stopped.activations == 1

    await runtime.drain()
    finished = runtime.last_drain
    assert finished.idle
    assert finished.remaining_deliveries == 0
    assert finished.remaining_runnable_processes == 0
    runtime.close()


async def test_a_default_budget_can_be_set_once_on_the_runtime(tmp_path):
    """Spec §46: a deployment may cap every call without touching call sites."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=6)
    runtime.default_drain_budget = DrainBudget(max_activations=2)

    await runtime.submit_event(step(0))

    assert len(RECORDED) == 2
    assert runtime.last_drain.exhausted
    runtime.close()


async def test_a_budget_argument_overrides_the_runtime_default(tmp_path):
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=4)
    runtime.default_drain_budget = DrainBudget(max_activations=1)

    await runtime.submit_event(step(0), UNLIMITED)

    assert len(RECORDED) == 4
    assert not runtime.last_drain.exhausted
    runtime.close()


async def test_a_budget_is_a_call_parameter_not_stored_state(tmp_path):
    """AT39 (spec §102): what remains lives in the tables, not in a budget."""
    runtime, _ = drain_runtime(tmp_path, "budget.db")
    register_chain(runtime, length=5)
    await runtime.submit_event(step(0), DrainBudget(max_activations=2))
    remaining = pending(runtime)
    runtime.close()

    # A new runtime, told nothing about the earlier budget, finishes the work.
    from drain_helpers import register_chain as rechain
    from nexus_seed.runtime.runtime import Runtime

    runtime2 = Runtime(tmp_path / "budget.db")
    rechain(runtime2, length=5)
    assert pending(runtime2) == remaining
    await runtime2.run_pending()

    assert [r["n"] for r in RECORDED] == [0, 1, 2, 3, 4]
    assert pending(runtime2) == 0
    runtime2.close()


async def test_tick_takes_a_budget_too(tmp_path):
    """AT40: the timer path is the one an outer loop actually calls."""
    from datetime import timedelta

    from nexus_seed.storage.timer_store import TimerRecord

    runtime, clock = drain_runtime(tmp_path)
    register_chain(runtime, length=5)
    runtime.timer_store.save(
        TimerRecord(
            fire_at=clock.now() + timedelta(seconds=1),
            event_type="step",
            payload={"n": 0},
        )
    )

    clock.advance(2)
    await runtime.tick(DrainBudget(max_activations=2))

    assert len(RECORDED) == 2
    assert runtime.last_drain.exhausted

    while runtime.last_drain.has_remaining:
        await runtime.tick(DrainBudget(max_activations=2))
    assert [r["n"] for r in RECORDED] == [0, 1, 2, 3, 4]
    runtime.close()


async def test_a_zero_budget_does_nothing_and_says_so(tmp_path):
    """AT41: a budget of nothing is a legal, well-behaved slice."""
    runtime, _ = drain_runtime(tmp_path)
    register_chain(runtime, length=3)

    await runtime.submit_event(step(0), DrainBudget(max_cycles=0))

    assert RECORDED == []
    assert runtime.last_drain.exhausted
    # The work was not lost, merely not started.
    assert pending(runtime) == 1
    await runtime.drain()
    assert [r["n"] for r in RECORDED] == [0, 1, 2]
    runtime.close()
