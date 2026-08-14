"""Decision and plan execution converge across arbitrarily small drain slices."""

from __future__ import annotations

import pytest

from decision_helpers import WorkStatus, choice_work, planning_runtime, register_alternatives
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.drain import DrainBudget
from planning_helpers import status_of, work_required


@pytest.mark.asyncio
async def test_selection_chain_converges_one_activation_at_a_time(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    requirement = choice_work(runtime)
    budget = DrainBudget(max_activations=1)

    await runtime.submit_event(work_required(requirement), budget=budget)
    for _ in range(100):
        if (
            status_of(runtime, requirement) is WorkStatus.SATISFIED
            and runtime.get_pending_event_delivery_count() == 0
            and not runtime.process_store.instances_by_status(ProcessStatus.RUNNABLE)
        ):
            break
        await runtime.drain(budget)
    else:  # pragma: no cover - diagnostic guard
        raise AssertionError("bounded decision chain did not converge")

    assert len(runtime.get_plan_selections(requirement.id)) == 1
    assert runtime.get_pending_event_delivery_count() == 0
    assert runtime.last_drain.activations <= 1
    runtime.close()
