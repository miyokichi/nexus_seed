"""A committed plan failure is replanned from durable delivery after restart."""

from __future__ import annotations

import pytest

from decision_helpers import WorkStatus, choice_work, planning_runtime, register_alternatives
from nexus_seed.delivery.models import EventDeliveryStatus
from nexus_seed.runtime.drain import DrainBudget
from planning_helpers import failing_worker, status_of, work_required


@pytest.mark.asyncio
async def test_pending_replan_event_survives_restart(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime, fast_handler=failing_worker)
    requirement = choice_work(runtime)
    one_activation = DrainBudget(max_activations=1)

    await runtime.submit_event(work_required(requirement), budget=one_activation)
    for _ in range(100):
        events = runtime.event_store.by_type("replan_required")
        if events:
            break
        await runtime.drain(one_activation)
    else:  # pragma: no cover - diagnostic guard
        raise AssertionError("plan never reached its terminal failure")

    replan_event = events[0]
    assert runtime.get_event_delivery(replan_event.id).status is EventDeliveryStatus.PENDING
    runtime.close()

    restarted = planning_runtime(tmp_path)
    register_alternatives(restarted, fast_handler=failing_worker)
    await restarted.run_pending()

    assert status_of(restarted, requirement) is WorkStatus.SATISFIED
    assert len(restarted.get_replan_attempts(requirement.id)) == 1
    assert restarted.get_event_delivery(replan_event.id).status is EventDeliveryStatus.DELIVERED
    assert restarted.get_pending_event_delivery_count() == 0
    restarted.close()
