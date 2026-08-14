"""AT53–AT58 (spec §50–§53): a composed plan spread across several slices.

This is where the two halves of Phase 4B.1 meet.  A plan is exactly the kind of
work that used to make a drain long: several stages, each producing the events
that start the next.  Running one across budget boundaries is the real test of
both — the plan must not care where the slices fell, and the bindings must
still be resolved from what is on disk.
"""

from __future__ import annotations

from drain_helpers import DrainBudget
from planning_helpers import (
    RECORDED,
    PlanStatus,
    WorkStatus,
    instances_named,
    make_branching_work,
    make_work,
    node_statuses,
    only_plan,
    planning_runtime,
    received_by,
    register_branching,
    register_chain,
    status_of,
    work_required,
)


async def finish(runtime, budget, *, limit: int = 200):
    """Drain in slices of ``budget`` until the runtime is genuinely idle."""
    for _ in range(limit):
        if runtime.last_drain is not None and runtime.last_drain.idle:
            return
        await runtime.drain(budget)
    raise AssertionError("the plan never went idle")


async def test_a_plan_completes_across_many_small_slices(tmp_path):
    """AT53."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    await runtime.submit_event(work_required(work), DrainBudget(max_activations=1))
    await finish(runtime, DrainBudget(max_activations=1))

    plan = only_plan(runtime)
    assert plan.status is PlanStatus.COMPLETED
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_a_slice_boundary_does_not_re_run_a_node(tmp_path):
    """AT54: yielding is not a restart, and a restart would not re-run either."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    await runtime.submit_event(work_required(work), DrainBudget(max_activations=1))
    await finish(runtime, DrainBudget(max_activations=1))

    for name in ("extract_measurement", "analyze_resistance", "generate_analysis_report"):
        assert len(instances_named(runtime, name)) == 1
    assert [r["node"] for r in RECORDED] == [
        "extract_measurement:v1",
        "analyze_resistance:v1",
        "generate_analysis_report:v1",
    ]
    runtime.close()


async def test_a_plan_stopped_mid_way_is_left_consistent(tmp_path):
    """AT55: no node is FAILED, no plan is abandoned (Invariant 71)."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    budget = DrainBudget(max_activations=1)
    await runtime.submit_event(work_required(work), budget)
    # Stop as soon as the plan exists and its first node has been spawned.
    for _ in range(40):
        plans = runtime.get_plans()
        if plans and any(
            n.process_instance_id for n in runtime.get_plan_nodes(plans[0].id)
        ):
            break
        await runtime.drain(budget)

    plan = only_plan(runtime)
    statuses = node_statuses(runtime, plan)
    assert plan.status in (PlanStatus.VALIDATED, PlanStatus.RUNNING)
    assert "FAILED" not in statuses.values()
    assert "PENDING" in statuses.values()  # genuinely mid-way
    assert runtime.last_drain.exhausted
    assert status_of(runtime, work) is not WorkStatus.CANCELLED
    runtime.close()


async def test_bindings_hold_across_a_slice_boundary(tmp_path):
    """AT56: each stage resolves its inputs from what is durably recorded."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    await runtime.submit_event(work_required(work), DrainBudget(max_activations=1))
    await finish(runtime, DrainBudget(max_activations=1))

    assert received_by("analyze_resistance:v1") == {
        "measurement": "measurement-from-extract_measurement"
    }
    assert received_by("generate_analysis_report:v1") == {
        "resistance_analysis": "resistance_analysis-from-analyze_resistance"
    }
    runtime.close()


async def test_a_branching_plan_survives_slicing_too(tmp_path):
    """AT57: the diamond, one activation at a time."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    work = make_branching_work(runtime)

    await runtime.submit_event(work_required(work), DrainBudget(max_activations=1))
    await finish(runtime, DrainBudget(max_activations=1))

    plan = only_plan(runtime)
    assert set(node_statuses(runtime, plan).values()) == {"COMPLETED"}
    assert received_by("generate_analysis_report:v1") == {
        "resistance_analysis": "resistance_analysis-from-analyze_resistance",
        "thermal_analysis": "thermal_analysis-from-analyze_thermal",
    }
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_the_slice_size_does_not_change_the_outcome(tmp_path):
    """AT58: determinism is not a property of how the caller paced the runtime."""
    outcomes = []
    for index, size in enumerate((1, 2, 3, 100)):
        runtime = planning_runtime(tmp_path, f"slice{index}.db")
        register_branching(runtime)
        work = make_branching_work(runtime)
        budget = DrainBudget(max_activations=size)

        await runtime.submit_event(work_required(work), budget)
        await finish(runtime, budget)

        plan = only_plan(runtime)
        outcomes.append(
            (
                plan.status,
                node_statuses(runtime, plan),
                received_by("generate_analysis_report:v1"),
                status_of(runtime, work),
            )
        )
        runtime.close()

    assert all(o == outcomes[0] for o in outcomes)
    assert outcomes[0][0] is PlanStatus.COMPLETED
