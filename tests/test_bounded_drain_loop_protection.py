"""AT48–AT52 (spec §48–§50): a budget is a bound, not a hint.

The case that forced this phase to include bounded drain at all: a process
whose own output re-triggers it.  Before, that was a hang — ``submit_event``
never returned, and no amount of care elsewhere in the system could recover a
runtime stuck inside its own drain loop.

A budget does not *fix* an infinite chain.  It makes the runtime survive one:
the loop is still infinite, but it is now infinite across calls the caller
controls, and every intermediate state is on disk.
"""

from __future__ import annotations

from drain_helpers import (
    RECORDED,
    DrainBudget,
    drain_runtime,
    pending,
    register_endless,
    step,
)


async def test_a_self_triggering_process_does_not_hang_the_runtime(tmp_path):
    """AT48: the whole reason bounded drain is in this phase."""
    runtime, _ = drain_runtime(tmp_path)
    register_endless(runtime)

    await runtime.submit_event(step(0), DrainBudget(max_activations=5))

    assert len(RECORDED) == 5
    assert runtime.last_drain.exhausted
    assert runtime.last_drain.has_remaining  # it will never stop wanting to run
    runtime.close()


async def test_the_endless_chain_is_still_endless_and_that_is_fine(tmp_path):
    """AT49: the budget bounds the call, not the workload (spec §49)."""
    runtime, _ = drain_runtime(tmp_path)
    register_endless(runtime)

    await runtime.submit_event(step(0), DrainBudget(max_activations=3))
    for _ in range(3):
        await runtime.drain(DrainBudget(max_activations=3))

    # It never runs out of work, and it never stops responding either.
    assert len(RECORDED) == 12
    assert runtime.last_drain.has_remaining
    assert pending(runtime) >= 1
    runtime.close()


async def test_a_cycle_budget_bounds_work_that_produces_neither_half(tmp_path):
    """AT50: a budget that cannot expire is not a budget.

    ``max_cycles`` is the backstop for a loop that neither dispatches nor
    activates.  Nothing in the system does that today, which is exactly why it
    is worth having a limit that does not depend on that staying true.
    """
    runtime, _ = drain_runtime(tmp_path)
    register_endless(runtime)

    await runtime.submit_event(step(0), DrainBudget(max_cycles=3))

    assert runtime.last_drain.cycles == 3
    assert runtime.last_drain.exhausted
    runtime.close()


async def test_the_lowest_limit_is_the_one_that_stops_the_slice(tmp_path):
    """AT51: dimensions combine, they do not override one another."""
    runtime, _ = drain_runtime(tmp_path)
    register_endless(runtime)

    await runtime.submit_event(
        step(0), DrainBudget(max_dispatches=99, max_activations=2, max_cycles=99)
    )

    assert runtime.last_drain.activations == 2
    assert runtime.last_drain.exhausted
    runtime.close()


async def test_an_interrupted_endless_chain_resumes_after_a_restart(tmp_path):
    """AT52: even a runaway leaves a consistent, restartable state."""
    from nexus_seed.runtime.runtime import Runtime

    runtime, _ = drain_runtime(tmp_path, "endless.db")
    register_endless(runtime)
    await runtime.submit_event(step(0), DrainBudget(max_activations=3))
    owed = pending(runtime)
    reached = max(r["n"] for r in RECORDED)
    runtime.close()

    RECORDED.clear()
    runtime2 = Runtime(tmp_path / "endless.db")
    register_endless(runtime2)
    assert pending(runtime2) == owed

    await runtime2.drain(DrainBudget(max_activations=2))

    # It picked up the chain rather than starting it again.
    assert min(r["n"] for r in RECORDED) > reached
    runtime2.close()
