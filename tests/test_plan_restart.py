"""AT14, AT15, AT16, AT17, AT18, AT29, AT30 (spec §109–§113, §124, §125).

Restart and failure, which for a composed plan means one thing above all:
**a node that already ran must not run again** (Invariant 61).  Nodes have side
effects; re-running the first step of a plan because the third one was
interrupted would be a new bug class, not a recovery.
"""

from __future__ import annotations

from planning_helpers import (
    RECORDED,
    PlanNodeStatus,
    PlanStatus,
    WorkStatus,
    failing_worker,
    instances_named,
    make_work,
    node_statuses,
    offer_work,
    only_plan,
    planning_runtime,
    register_chain,
    register_step,
    staged_worker,
    status_of,
    wire,
)

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


def rebuild(tmp_path, name="plan.db"):
    """Rebuild a runtime over the same database, with the chain registered."""
    runtime = wire(Runtime(tmp_path / name))
    register_chain(runtime)
    return runtime


async def test_a_completed_node_is_not_re_run_after_a_restart(tmp_path):
    """AT14: the central claim."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    # Compose, then run only the first stage before "crashing".
    runtime.registry._handlers.pop("execute_process_plan", None)
    await offer_work(runtime, work)
    plan = only_plan(runtime)

    from nexus_seed.processes.planning import EXECUTE_PROCESS_PLAN, execute_process_plan

    runtime.register_process(EXECUTE_PROCESS_PLAN, execute_process_plan)
    # Drive one activation of the executor: it spawns stage 1 and suspends.
    runtime.event_store.append(
        Event("process_plan_created", "test", {"plan_id": str(plan.id)})
    )
    runtime.dispatch_pending_events()
    executor = runtime.scheduler.next_runnable()
    await runtime.executor.execute(executor)

    statuses = node_statuses(runtime, plan)
    assert statuses["extract_measurement:v1"] == "RUNNING"
    assert statuses["analyze_resistance:v1"] == "PENDING"
    runtime.close()

    # --- rebuilt; finish the plan ---
    RECORDED.clear()
    runtime2 = rebuild(tmp_path)
    await runtime2.run_pending()

    assert runtime2.get_plan(plan.id).status is PlanStatus.COMPLETED
    assert status_of(runtime2, work) is WorkStatus.SATISFIED
    # Exactly one instance per node, across both runtimes.
    for name in ("extract_measurement", "analyze_resistance", "generate_analysis_report"):
        assert len(instances_named(runtime2, name)) == 1
    runtime2.close()


async def test_repeated_restarts_converge_on_one_of_everything(tmp_path):
    """AT30: rebuild the runtime between every unit of work."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)
    runtime.event_store.append(
        Event("work_required", "test", {"work_requirement_id": str(work.id)})
    )
    runtime.close()

    for _ in range(60):
        runtime = rebuild(tmp_path)
        runtime.dispatch_pending_events(limit=1)
        instance = runtime.scheduler.next_runnable()
        if instance is not None:
            await runtime.executor.execute(instance)
            # One unit of work, exactly as the drain loop does it: a finished
            # instance may satisfy a join, which is how a plan stage advances.
            for join_event in runtime.join_coordinator.on_instance_finished(instance):
                runtime.event_store.append(join_event)
        idle = (
            runtime.get_pending_event_delivery_count() == 0
            and runtime.scheduler.next_runnable() is None
        )
        runtime.close()
        if idle:
            break

    runtime = rebuild(tmp_path)
    await runtime.run_pending()

    assert len(runtime.get_work_requirements()) == 1
    assert len(runtime.get_plans()) == 1
    for name in ("extract_measurement", "analyze_resistance", "generate_analysis_report"):
        assert len(instances_named(runtime, name)) == 1
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_a_node_is_not_re_spawned_by_a_repeated_activation(tmp_path):
    """AT16: node identity is a position, and a position is filled once."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)
    await offer_work(runtime, work)
    plan = only_plan(runtime)

    # Re-deliver the plan-created event: a second executor starts on a plan
    # that is already finished.
    await runtime.submit_event(
        Event("process_plan_created", "test", {"plan_id": str(plan.id)})
    )

    for name in ("extract_measurement", "analyze_resistance", "generate_analysis_report"):
        assert len(instances_named(runtime, name)) == 1
    assert runtime.get_plan(plan.id).status is PlanStatus.COMPLETED
    runtime.close()


async def test_a_running_node_is_recovered_not_duplicated(tmp_path):
    """AT15: Phase 2A recovery reclaims the instance; the node keeps it."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)
    runtime.registry._handlers.pop("execute_process_plan", None)
    await offer_work(runtime, work)
    plan = only_plan(runtime)

    from nexus_seed.processes.planning import EXECUTE_PROCESS_PLAN, execute_process_plan

    runtime.register_process(EXECUTE_PROCESS_PLAN, execute_process_plan)
    runtime.event_store.append(
        Event("process_plan_created", "test", {"plan_id": str(plan.id)})
    )
    runtime.dispatch_pending_events()
    await runtime.executor.execute(runtime.scheduler.next_runnable())

    # The first node's process is interrupted mid-activation.
    node = [n for n in runtime.get_plan_nodes(plan.id) if n.node_key.startswith("extract")][0]
    child = runtime.process_store.get_instance(node.process_instance_id)
    child.status = ProcessStatus.RUNNING
    runtime.process_store.save_instance(child)
    runtime.close()

    runtime2 = rebuild(tmp_path)
    # Startup recovery returned it to RUNNABLE — the same instance.
    assert runtime2.process_store.get_instance(child.id).status is ProcessStatus.RUNNABLE
    await runtime2.run_pending()

    assert len(instances_named(runtime2, "extract_measurement")) == 1
    assert runtime2.get_plan(plan.id).status is PlanStatus.COMPLETED
    runtime2.close()


async def test_a_terminal_node_failure_fails_the_plan(tmp_path):
    """AT17: the later stages do not run, and the earlier results stand."""
    runtime = planning_runtime(tmp_path)
    register_step(
        runtime, "extract_measurement", "extract_measurement",
        ("raw_measurement_resource",), ("measurement",),
    )
    register_step(
        runtime, "analyze_resistance", "analyze_resistance",
        ("measurement",), ("resistance_analysis",), handler=failing_worker,
    )
    register_step(
        runtime, "generate_analysis_report", "generate_analysis_report",
        ("resistance_analysis",), ("analysis_report",),
    )
    work = make_work(runtime)

    await offer_work(runtime, work)

    plan = only_plan(runtime)
    assert runtime.get_plan(plan.id).status is PlanStatus.FAILED
    statuses = node_statuses(runtime, plan)
    assert statuses["extract_measurement:v1"] == "COMPLETED"
    assert statuses["analyze_resistance:v1"] == "FAILED"
    assert statuses["generate_analysis_report:v1"] == "PENDING"
    assert instances_named(runtime, "generate_analysis_report") == []

    # The need is not cancelled (Invariant 63).
    assert status_of(runtime, work) is not WorkStatus.CANCELLED
    assert status_of(runtime, work) is not WorkStatus.SATISFIED
    runtime.close()


async def test_no_compensation_is_generated_for_earlier_nodes(tmp_path):
    """AT18: whatever the first node did stays done, and is not undone."""
    runtime = planning_runtime(tmp_path)
    register_step(
        runtime, "extract_measurement", "extract_measurement",
        ("raw_measurement_resource",), ("measurement",),
    )
    register_step(
        runtime, "analyze_resistance", "analyze_resistance",
        ("measurement",), ("resistance_analysis",), handler=failing_worker,
    )
    register_step(
        runtime, "generate_analysis_report", "generate_analysis_report",
        ("resistance_analysis",), ("analysis_report",),
    )
    await offer_work(runtime, make_work(runtime))

    first = instances_named(runtime, "extract_measurement")[0]
    assert first.status is ProcessStatus.COMPLETED
    assert first.local_state["output"]["outputs"][0]["type"] == "measurement"
    # No rollback machinery of any kind was invoked.
    assert runtime.get_action_proposals() == []
    assert runtime.event_store.by_type("process_plan_failed") != []
    runtime.close()


async def test_a_plan_that_completes_without_the_output_does_not_satisfy(tmp_path):
    """AT20: all nodes done is not the same as the work being done.

    The plan is valid on paper — the last step *declares* it produces a report.
    At run time its handler returns an untyped result, so nothing actually
    produced one.  Completing every node must not be allowed to mean SATISFIED
    on its own (spec §60).
    """

    async def silent_worker(ctx):
        # Completes successfully, but declares no typed output.
        return ctx.complete(output={"note": "did something, said nothing"})

    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("raw",), ("measurement",))
    register_step(
        runtime, "p2", "b", ("measurement",), ("analysis_report",), handler=silent_worker
    )
    work = make_work(
        runtime, required=("a", "b"), inputs=("raw",), outputs=("analysis_report",)
    )

    await offer_work(runtime, work)

    plan = only_plan(runtime)
    assert node_statuses(runtime, plan) == {"p1:v1": "COMPLETED", "p2:v1": "COMPLETED"}
    assert runtime.get_plan(plan.id).status is PlanStatus.COMPLETED

    # ...but the work is not satisfied, and the reason says why.
    assert status_of(runtime, work) is not WorkStatus.SATISFIED
    completed = runtime.event_store.by_type("process_plan_completed")[-1]
    assert completed.payload["satisfied"] is False
    assert any("required outputs" in r for r in completed.payload["reasons"])
    runtime.close()
