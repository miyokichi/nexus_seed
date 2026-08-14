"""Terminal plan failure reconsiders the need without erasing history."""

from __future__ import annotations

import pytest

from decision_helpers import (
    PlanStatus,
    WorkStatus,
    choice_work,
    offer_work,
    planning_runtime,
    register_alternatives,
    status_of,
)
from nexus_seed.planning.fingerprint import plan_fingerprint
from nexus_seed.processes.planning import REPLAN_REQUIRED
from planning_helpers import failing_worker


@pytest.mark.asyncio
async def test_terminal_failure_selects_a_fresh_alternative(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime, fast_handler=failing_worker)
    requirement = choice_work(runtime)

    await offer_work(runtime, requirement)

    plans = runtime.get_plans_for_work(requirement.id)
    failed = [plan for plan in plans if plan.status is PlanStatus.FAILED]
    completed = [plan for plan in plans if plan.status is PlanStatus.COMPLETED]
    selections = runtime.get_plan_selections(requirement.id)
    attempts = runtime.get_replan_attempts(requirement.id)
    assert len(failed) == 1
    assert len(completed) == 1
    assert failed[0].id != completed[0].id
    assert failed[0].fingerprint != completed[0].fingerprint
    assert len(selections) == 2
    assert selections[0].selected_plan_id == failed[0].id
    assert selections[1].selected_plan_id == completed[0].id
    assert len(attempts) == 1
    assert attempts[0].previous_plan_id == failed[0].id
    assert failed[0].fingerprint in attempts[0].excluded_fingerprints
    assert status_of(runtime, requirement) is WorkStatus.SATISFIED
    assert runtime.work_requirement_store.get(requirement.id).selected_plan_id == completed[0].id
    runtime.close()


@pytest.mark.asyncio
async def test_replan_does_not_mutate_the_failed_plan_graph(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime, fast_handler=failing_worker)
    requirement = choice_work(runtime)

    await offer_work(runtime, requirement)

    failed = next(
        plan for plan in runtime.get_plans_for_work(requirement.id)
        if plan.status is PlanStatus.FAILED
    )
    nodes = runtime.get_plan_nodes(failed.id)
    edges = runtime.plan_store.edges(failed.id)
    assert plan_fingerprint(nodes, edges) == failed.fingerprint
    assert any(node.status.value == "FAILED" for node in nodes)
    runtime.close()


@pytest.mark.asyncio
async def test_redelivered_replan_converges_without_another_plan(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime, fast_handler=failing_worker)
    requirement = choice_work(runtime)
    await offer_work(runtime, requirement)
    failed = next(
        plan for plan in runtime.get_plans_for_work(requirement.id)
        if plan.status is PlanStatus.FAILED
    )
    plan_count = len(runtime.get_plans_for_work(requirement.id))
    selection_count = len(runtime.get_plan_selections(requirement.id))

    from nexus_seed.core.event import Event

    await runtime.submit_event(
        Event(
            REPLAN_REQUIRED,
            "redelivery-test",
            {"plan_id": str(failed.id), "work_requirement_id": str(requirement.id)},
        )
    )

    assert len(runtime.get_replan_attempts(requirement.id)) == 1
    assert len(runtime.get_plans_for_work(requirement.id)) == plan_count
    assert len(runtime.get_plan_selections(requirement.id)) == selection_count
    runtime.close()
