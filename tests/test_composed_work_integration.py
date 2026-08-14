"""AT6, AT11, AT12, AT19 (spec §101, §106, §107, §114): compose and run.

The demo chain from the spec: no single process can produce an analysis report
from a raw measurement resource, but three of them in the right order can.
"""

from __future__ import annotations

from planning_helpers import (
    RECORDED,
    PlanStatus,
    WorkStatus,
    instances_named,
    make_work,
    node_statuses,
    offer_work,
    only_plan,
    planning_runtime,
    register_chain,
    status_of,
)


async def test_a_three_step_chain_is_composed_and_run(tmp_path):
    """AT6 + AT8 + AT11: composition required, planned, executed, satisfied."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    await offer_work(runtime, work)

    plan = only_plan(runtime)
    assert plan.status is PlanStatus.COMPLETED
    assert [n.node_key for n in runtime.get_plan_nodes(plan.id)] == [
        "extract_measurement:v1",
        "analyze_resistance:v1",
        "generate_analysis_report:v1",
    ]
    assert node_statuses(runtime, plan) == {
        "extract_measurement:v1": "COMPLETED",
        "analyze_resistance:v1": "COMPLETED",
        "generate_analysis_report:v1": "COMPLETED",
    }
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_the_nodes_run_in_dependency_order(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    assert [r["node"] for r in RECORDED] == [
        "extract_measurement:v1",
        "analyze_resistance:v1",
        "generate_analysis_report:v1",
    ]
    runtime.close()


async def test_typed_outputs_propagate_down_the_chain(tmp_path):
    """AT12: each step receives what the previous one produced."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    by_node = {r["node"]: r["received"] for r in RECORDED}
    assert by_node["extract_measurement:v1"] == {}
    assert by_node["analyze_resistance:v1"]["measurement"] == (
        "measurement-from-extract_measurement"
    )
    assert by_node["generate_analysis_report:v1"]["resistance_analysis"] == (
        "resistance_analysis-from-analyze_resistance"
    )
    runtime.close()


async def test_each_node_runs_exactly_one_process(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    for name in ("extract_measurement", "analyze_resistance", "generate_analysis_report"):
        assert len(instances_named(runtime, name)) == 1
    runtime.close()


async def test_the_plan_records_its_edges(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    trace = runtime.get_plan_trace(only_plan(runtime).id)
    assert trace.edge_pairs == [
        ("extract_measurement:v1", "analyze_resistance:v1", "measurement"),
        ("analyze_resistance:v1", "generate_analysis_report:v1", "resistance_analysis"),
    ]
    runtime.close()


async def test_a_single_capable_process_never_reaches_the_planner(tmp_path):
    """AT5: Phase 4A's path is untouched."""
    from planning_helpers import register_step

    runtime = planning_runtime(tmp_path)
    register_step(
        runtime, "does_it_all", "everything", ("raw_measurement_resource",), ("analysis_report",)
    )
    work = make_work(runtime, required=("everything",))

    await offer_work(runtime, work)

    assert runtime.get_plans() == []
    assert runtime.event_store.by_type("composition_required") == []
    assert len(instances_named(runtime, "does_it_all")) == 1
    runtime.close()


async def test_a_missing_capability_is_not_a_composition_problem(tmp_path):
    """AT7: nothing provides it, so there is nothing to arrange."""
    from planning_helpers import register_step

    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", (), ("x",))
    work = make_work(runtime, required=("a", "nobody_provides_this"), inputs=(), outputs=())

    await offer_work(runtime, work)

    assert runtime.get_plans() == []
    assert runtime.event_store.by_type("composition_required") == []
    assert runtime.event_store.by_type("capability_missing") != []
    assert status_of(runtime, work) is WorkStatus.BLOCKED_CAPABILITY
    runtime.close()


async def test_the_work_passes_through_planned_on_its_way(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    await offer_work(runtime, work)

    # The composer produced candidates; the selector chose; the executor
    # finished the job.  Since Phase 4C the composer reports options, not a
    # decision, so its output is a list of orders rather than one node count.
    composer = instances_named(runtime, "compose_work_plan")[0]
    assert composer.local_state["output"]["orders"][0] == [
        "extract_measurement:v1",
        "analyze_resistance:v1",
        "generate_analysis_report:v1",
    ]
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_plan_events_are_emitted_and_delivered(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    for event_type in ("composition_required", "process_plan_created", "process_plan_completed"):
        events = runtime.event_store.by_type(event_type)
        assert len(events) == 1
        assert runtime.get_event_delivery(events[0].id).status.value == "DELIVERED"
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()
