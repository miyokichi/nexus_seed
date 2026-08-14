"""AT73–AT78 (spec §65–§68): everything above, across a restart.

The claim a durable system has to make is not that it works, but that it works
when it is interrupted.  Phase 4B.1 adds two things a restart can break:
bindings, which must be read back rather than recomputed, and budgets, which
must leave a state the next process can pick up without knowing a budget was
ever involved.
"""

from __future__ import annotations

from drain_helpers import DrainBudget
from planning_helpers import (
    RECORDED,
    PlanStatus,
    WorkStatus,
    instances_named,
    make_branching_work,
    node_statuses,
    only_plan,
    plan_edges,
    planning_runtime,
    received_by,
    register_branching,
    status_of,
    wire,
    work_required,
)

from nexus_seed.runtime.runtime import Runtime

DB = "hardening.db"
BRANCH_NODES = (
    "extract_measurement",
    "analyze_resistance",
    "analyze_thermal",
    "generate_analysis_report",
)


def rebuild(tmp_path):
    runtime = wire(Runtime(tmp_path / DB))
    register_branching(runtime)
    return runtime


async def sliced_until(runtime, predicate, *, size: int = 1, limit: int = 200):
    """Drain one activation at a time until ``predicate`` holds."""
    budget = DrainBudget(max_activations=size)
    for _ in range(limit):
        if predicate(runtime):
            return True
        await runtime.drain(budget)
    return predicate(runtime)


def plan_exists_and_has_started(runtime) -> bool:
    plans = runtime.get_plans()
    if not plans:
        return False
    return any(n.process_instance_id for n in runtime.get_plan_nodes(plans[0].id))


async def test_a_sliced_run_interrupted_mid_plan_finishes_after_a_restart(tmp_path):
    """AT73: the budget and the crash together."""
    runtime = planning_runtime(tmp_path, DB)
    register_branching(runtime)
    work = make_branching_work(runtime)

    await runtime.submit_event(work_required(work), DrainBudget(max_activations=1))
    assert await sliced_until(runtime, plan_exists_and_has_started)
    plan_id = only_plan(runtime).id
    runtime.close()

    runtime2 = rebuild(tmp_path)
    await runtime2.run_pending()

    assert runtime2.get_plan(plan_id).status is PlanStatus.COMPLETED
    assert status_of(runtime2, work) is WorkStatus.SATISFIED
    for name in BRANCH_NODES:
        assert len(instances_named(runtime2, name)) == 1
    runtime2.close()


async def test_the_restarted_runtime_resolves_bindings_from_disk(tmp_path):
    """AT74: the runtime that finishes the plan never saw the planner run."""
    runtime = planning_runtime(tmp_path, DB)
    register_branching(runtime)
    work = make_branching_work(runtime)

    await runtime.submit_event(work_required(work), DrainBudget(max_activations=1))
    assert await sliced_until(runtime, plan_exists_and_has_started)
    runtime.close()

    RECORDED.clear()
    runtime2 = rebuild(tmp_path)
    await runtime2.run_pending()

    assert received_by("generate_analysis_report:v1") == {
        "resistance_analysis": "resistance_analysis-from-analyze_resistance",
        "thermal_analysis": "thermal_analysis-from-analyze_thermal",
    }
    runtime2.close()


async def test_the_bindings_themselves_are_unchanged_by_the_restart(tmp_path):
    """AT75: a plan is a decision already made (Invariant 57)."""
    runtime = planning_runtime(tmp_path, DB)
    register_branching(runtime)
    work = make_branching_work(runtime)
    await runtime.submit_event(work_required(work))
    plan_id = only_plan(runtime).id
    before = sorted(e.describe() for e in plan_edges(runtime, only_plan(runtime)))
    before_trace = sorted(runtime.get_plan_trace(plan_id).binding_pairs)
    runtime.close()

    runtime2 = rebuild(tmp_path)

    assert sorted(e.describe() for e in runtime2.plan_store.edges(plan_id)) == before
    assert sorted(runtime2.get_plan_trace(plan_id).binding_pairs) == before_trace
    runtime2.close()


async def test_a_budget_leaves_nothing_a_restart_has_to_know_about(tmp_path):
    """AT76: a budget is a call parameter, not durable state (spec §102)."""
    runtime = planning_runtime(tmp_path, DB)
    register_branching(runtime)
    work = make_branching_work(runtime)

    await runtime.submit_event(work_required(work), DrainBudget(max_activations=2))
    owed = runtime.get_pending_event_delivery_count()
    runtime.close()

    # The new runtime is told nothing; the tables carry the whole truth.
    runtime2 = rebuild(tmp_path)
    assert runtime2.get_pending_event_delivery_count() == owed
    assert runtime2.default_drain_budget.unlimited

    await runtime2.run_pending()

    assert only_plan(runtime2).status is PlanStatus.COMPLETED
    assert runtime2.get_pending_event_delivery_count() == 0
    runtime2.close()


async def test_restarting_between_every_slice_still_converges(tmp_path):
    """AT77: rebuild the runtime after each budgeted slice."""
    runtime = planning_runtime(tmp_path, DB)
    register_branching(runtime)
    work = make_branching_work(runtime)
    runtime.event_store.append(work_required(work))
    runtime.close()

    for _ in range(120):
        runtime = rebuild(tmp_path)
        await runtime.drain(DrainBudget(max_activations=1))
        done = runtime.last_drain.idle
        runtime.close()
        if done:
            break

    runtime = rebuild(tmp_path)
    plan = only_plan(runtime)
    assert plan.status is PlanStatus.COMPLETED
    assert set(node_statuses(runtime, plan).values()) == {"COMPLETED"}
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    for name in BRANCH_NODES:
        assert len(instances_named(runtime, name)) == 1
    runtime.close()


async def test_the_outcome_does_not_depend_on_where_the_restarts_fell(tmp_path):
    """AT78: same plan, same bindings, same result, however it was paced."""
    outcomes = []
    for index, size in enumerate((1, 3, 1000)):
        db = f"paced{index}.db"
        runtime = planning_runtime(tmp_path, db)
        register_branching(runtime)
        work = make_branching_work(runtime)
        runtime.event_store.append(work_required(work))
        runtime.close()

        for _ in range(120):
            runtime = wire(Runtime(tmp_path / db))
            register_branching(runtime)
            await runtime.drain(DrainBudget(max_activations=size))
            done = runtime.last_drain.idle
            runtime.close()
            if done:
                break

        runtime = wire(Runtime(tmp_path / db))
        register_branching(runtime)
        plan = only_plan(runtime)
        outcomes.append(
            (
                plan.status,
                node_statuses(runtime, plan),
                sorted(runtime.get_plan_trace(plan.id).binding_pairs),
                status_of(runtime, work),
            )
        )
        runtime.close()

    assert all(o == outcomes[0] for o in outcomes)
    assert outcomes[0][0] is PlanStatus.COMPLETED
    assert outcomes[0][3] is WorkStatus.SATISFIED
