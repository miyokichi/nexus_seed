"""AT14–AT19 (spec §25–§30): the diamond, P1 -> {P2, P3} -> P4.

Phase 4B implemented parallel spawn and multi-input join and then only ever ran
a straight line, so both were untested code paths carried into a phase that
depends on them.  Making this shape an acceptance case is the point (spec §27):
a plan is a DAG, and until a DAG with real branching runs, "DAG" is a claim
about the validator, not about execution.
"""

from __future__ import annotations

from planning_helpers import (
    PlanNodeStatus,
    PlanStatus,
    RECORDED,
    instances_named,
    make_branching_work,
    node_statuses,
    offer_work,
    only_plan,
    plan_edges,
    planning_runtime,
    received_by,
    register_branching,
    status_of,
)

from nexus_seed.work.work_requirement import WorkStatus


async def test_the_diamond_runs_to_completion(tmp_path):
    """AT14."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    work = make_branching_work(runtime)

    await offer_work(runtime, work)

    plan = only_plan(runtime)
    assert plan.status is PlanStatus.COMPLETED
    assert node_statuses(runtime, plan) == {
        "extract_measurement:v1": "COMPLETED",
        "analyze_resistance:v1": "COMPLETED",
        "analyze_thermal:v1": "COMPLETED",
        "generate_analysis_report:v1": "COMPLETED",
    }
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_the_two_branches_are_laid_out_at_the_same_depth(tmp_path):
    """AT15: they are independent, so they are one stage, not two."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    depths = {n.node_key: n.depth for n in runtime.get_plan_nodes(only_plan(runtime).id)}

    assert depths["extract_measurement:v1"] == 0
    assert depths["analyze_resistance:v1"] == depths["analyze_thermal:v1"] == 1
    assert depths["generate_analysis_report:v1"] == 2
    runtime.close()


async def test_both_branches_are_spawned_in_one_stage(tmp_path):
    """AT16: parallel spawn, through the ordinary join (spec §37)."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    executor = instances_named(runtime, "execute_process_plan")[0]
    # Three stages for four nodes: the middle stage carried two.
    order = [r["node"] for r in RECORDED]
    assert order[0] == "extract_measurement:v1"
    assert set(order[1:3]) == {"analyze_resistance:v1", "analyze_thermal:v1"}
    assert order[3] == "generate_analysis_report:v1"
    assert executor.status.value == "COMPLETED"
    runtime.close()


async def test_one_output_feeds_both_branches(tmp_path):
    """AT17: fan-out — two edges may draw from the same producing port."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    trace = runtime.get_plan_trace(only_plan(runtime).id)
    from_extract = [
        b for b in trace.bindings if b.producer_node_key == "extract_measurement:v1"
    ]

    assert {b.consumer_node_key for b in from_extract} == {
        "analyze_resistance:v1",
        "analyze_thermal:v1",
    }
    assert received_by("analyze_resistance:v1") == received_by("analyze_thermal:v1")
    runtime.close()


async def test_the_join_node_receives_both_branch_results(tmp_path):
    """AT18: the case a linear chain could never produce."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    assert received_by("generate_analysis_report:v1") == {
        "resistance_analysis": "resistance_analysis-from-analyze_resistance",
        "thermal_analysis": "thermal_analysis-from-analyze_thermal",
    }
    runtime.close()


async def test_the_join_waits_for_the_slower_branch(tmp_path):
    """AT19: readiness is 'all predecessors completed', not 'any'."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    order = [r["node"] for r in RECORDED]
    report = order.index("generate_analysis_report:v1")

    assert report > order.index("analyze_resistance:v1")
    assert report > order.index("analyze_thermal:v1")
    runtime.close()


async def test_a_failing_branch_fails_the_plan_and_the_join_never_runs(tmp_path):
    """A DAG does not partially succeed into its join."""
    from planning_helpers import failing_worker, register_step

    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    register_step(
        runtime,
        "analyze_thermal",
        "analyze_thermal",
        ("measurement",),
        ("thermal_analysis",),
        handler=failing_worker,
    )
    work = make_branching_work(runtime)

    await offer_work(runtime, work)

    plan = only_plan(runtime)
    statuses = node_statuses(runtime, plan)
    assert plan.status is PlanStatus.FAILED
    assert statuses["analyze_thermal:v1"] == "FAILED"
    assert statuses["generate_analysis_report:v1"] == PlanNodeStatus.PENDING.value
    # Nothing is undone (spec §55): the branch that worked keeps its result.
    assert statuses["analyze_resistance:v1"] == "COMPLETED"
    assert status_of(runtime, work) is not WorkStatus.SATISFIED
    runtime.close()


async def test_the_diamond_is_recorded_as_four_bindings(tmp_path):
    """The plan's own account of its data flow."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    edges = plan_edges(runtime, only_plan(runtime))

    assert sorted(e.describe() for e in edges) == [
        "measurement -> measurement",
        "measurement -> measurement",
        "resistance_analysis -> resistance_analysis",
        "thermal_analysis -> thermal_analysis",
    ]
    assert all(e.is_bound for e in edges)
    runtime.close()
