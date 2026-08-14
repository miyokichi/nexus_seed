"""AT20–AT24 (spec §31–§33): a restart in the middle of a branching stage.

A linear plan has one node in flight at a time, so "resume from the first
position that has not finished" is unambiguous.  A diamond does not: a crash
can land with one branch completed and the other still running, and the
executor has to re-attach to the running one rather than start a second
(Invariant 61) while not re-running the finished one.

That is the case Phase 4B never produced, which is why it is here.
"""

from __future__ import annotations

from planning_helpers import (
    RECORDED,
    PlanStatus,
    WorkStatus,
    instances_named,
    make_branching_work,
    node_statuses,
    offer_work,
    only_plan,
    planning_runtime,
    received_by,
    register_branching,
    status_of,
    wire,
)

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime

BRANCH_NODES = (
    "extract_measurement",
    "analyze_resistance",
    "analyze_thermal",
    "generate_analysis_report",
)


def rebuild(tmp_path, name="branch.db"):
    """Rebuild a runtime over the same database, with the diamond registered."""
    runtime = wire(Runtime(tmp_path / name))
    register_branching(runtime)
    return runtime


async def step(runtime) -> bool:
    """Do one unit of work: route one event, run at most one process.

    Exactly what the drain loop does per iteration, unrolled so a test can stop
    at a chosen moment.  Returns whether anything ran.
    """
    runtime.dispatch_pending_events(limit=1)
    instance = runtime.scheduler.next_runnable()
    if instance is not None:
        await runtime.executor.execute(instance)
        for join_event in runtime.join_coordinator.on_instance_finished(instance):
            runtime.event_store.append(join_event)
        return True
    # Nothing ran, but an event still owed means the next step can do something.
    return runtime.get_pending_event_delivery_count() > 0


def instance_status(runtime, node):
    """The status of whatever is filling a plan position, if anything is."""
    if node.process_instance_id is None:
        return None
    instance = runtime.process_store.get_instance(node.process_instance_id)
    return instance.status if instance else None


def nodes_of(runtime, plan) -> dict:
    return {n.node_key: n for n in runtime.get_plan_nodes(plan.id)}


def branches_finished_but_not_absorbed(runtime, plan) -> bool:
    """Both branch processes are done; the executor has not woken up yet.

    The interesting instant, and the reason these predicates look at instances
    rather than node statuses: absorbing a stage and spawning the next one
    happen in a single transaction (Phase 2A), so "both branches COMPLETED,
    join still PENDING" is a state the plan table never shows.
    """
    nodes = nodes_of(runtime, plan)
    if nodes["generate_analysis_report:v1"].process_instance_id is not None:
        return False
    return all(
        instance_status(runtime, nodes[key]) is ProcessStatus.COMPLETED
        for key in ("analyze_resistance:v1", "analyze_thermal:v1")
    )


BRANCH_KEYS = ("analyze_resistance:v1", "analyze_thermal:v1")


def one_branch_finished(runtime, plan) -> bool:
    """Exactly one branch process has finished."""
    nodes = nodes_of(runtime, plan)
    done = [
        instance_status(runtime, nodes[key]) is ProcessStatus.COMPLETED
        for key in BRANCH_KEYS
    ]
    return done.count(True) == 1


def one_branch_still_waiting_to_run(runtime, plan) -> bool:
    """One branch is done; the other is spawned but has never been activated."""
    nodes = nodes_of(runtime, plan)
    statuses = [instance_status(runtime, nodes[key]) for key in BRANCH_KEYS]
    return (
        statuses.count(ProcessStatus.COMPLETED) == 1
        and statuses.count(ProcessStatus.RUNNABLE) == 1
    )


def finished_branch(runtime, plan) -> str:
    nodes = nodes_of(runtime, plan)
    for key in BRANCH_KEYS:
        if instance_status(runtime, nodes[key]) is ProcessStatus.COMPLETED:
            return key
    raise AssertionError("no branch has finished")


def unstarted_branch(runtime, plan) -> str:
    nodes = nodes_of(runtime, plan)
    for key in BRANCH_KEYS:
        if instance_status(runtime, nodes[key]) is ProcessStatus.RUNNABLE:
            return key
    raise AssertionError("every branch has started")


async def crash_after(tmp_path, predicate, *, limit: int = 80):
    """Run the diamond until ``predicate`` holds, and hand back the runtime."""
    runtime = planning_runtime(tmp_path, "branch.db")
    register_branching(runtime)
    work = make_branching_work(runtime)
    runtime.event_store.append(
        Event("work_required", "test", {"work_requirement_id": str(work.id)})
    )

    plan = None
    for _ in range(limit):
        if plan is None:
            plans = runtime.get_plans()
            plan = plans[0] if plans else None
        if plan is not None and predicate(runtime, plan):
            return runtime, work, plan
        if not await step(runtime):
            break

    raise AssertionError(
        f"never reached the wanted state: {node_statuses(runtime, plan) if plan else 'no plan'}"
    )


async def test_a_crash_mid_branch_still_finishes_the_plan(tmp_path):
    """AT20: one branch done, one still to run, then the process dies."""
    runtime, work, plan = await crash_after(tmp_path, one_branch_finished)
    runtime.close()

    runtime2 = rebuild(tmp_path)
    await runtime2.run_pending()

    assert runtime2.get_plan(plan.id).status is PlanStatus.COMPLETED
    assert status_of(runtime2, work) is WorkStatus.SATISFIED
    for name in BRANCH_NODES:
        assert len(instances_named(runtime2, name)) == 1
    runtime2.close()


async def test_a_branch_interrupted_mid_activation_is_re_attached(tmp_path):
    """AT21: the diamond's version of Invariant 61.

    The crash lands inside the second branch's first activation: one branch has
    finished, the other was spawned and never got to run.  Recovery must
    reclaim *that* instance, not create a second one for the same position.
    """
    runtime, work, plan = await crash_after(tmp_path, one_branch_still_waiting_to_run)
    waiting = unstarted_branch(runtime, plan)

    node = nodes_of(runtime, plan)[waiting]
    child = runtime.process_store.get_instance(node.process_instance_id)
    child.status = ProcessStatus.RUNNING  # interrupted, never committed
    runtime.process_store.save_instance(child)
    runtime.close()

    runtime2 = rebuild(tmp_path)
    # Startup recovery reclaimed the same instance rather than losing it.
    assert runtime2.process_store.get_instance(child.id).status is ProcessStatus.RUNNABLE

    await runtime2.run_pending()

    assert runtime2.plan_store.get_node(node.id).process_instance_id == child.id
    for name in BRANCH_NODES:
        assert len(instances_named(runtime2, name)) == 1
    assert runtime2.get_plan(plan.id).status is PlanStatus.COMPLETED
    runtime2.close()


async def test_the_completed_branch_is_not_re_run(tmp_path):
    """AT22: side effects do not get a second go because a sibling stalled."""
    runtime, work, plan = await crash_after(tmp_path, one_branch_finished)
    done = finished_branch(runtime, plan)
    runtime.close()

    RECORDED.clear()
    runtime2 = rebuild(tmp_path)
    await runtime2.run_pending()

    replayed = [r["node"] for r in RECORDED]
    assert "extract_measurement:v1" not in replayed
    assert done not in replayed
    for name in BRANCH_NODES:
        assert len(instances_named(runtime2, name)) == 1
    runtime2.close()


async def test_bindings_are_read_from_disk_after_the_restart(tmp_path):
    """AT23: the join still gets both values, resolved from stored edges.

    The point of durable bindings: the runtime that finishes the plan never saw
    the planner run.
    """
    runtime, work, plan = await crash_after(tmp_path, branches_finished_but_not_absorbed)
    runtime.close()

    RECORDED.clear()
    runtime2 = rebuild(tmp_path)
    await runtime2.run_pending()

    assert received_by("generate_analysis_report:v1") == {
        "resistance_analysis": "resistance_analysis-from-analyze_resistance",
        "thermal_analysis": "thermal_analysis-from-analyze_thermal",
    }
    assert runtime2.get_plan(plan.id).status is PlanStatus.COMPLETED
    runtime2.close()


async def test_the_diamond_survives_a_restart_at_every_step(tmp_path):
    """AT24: rebuild the runtime between every single unit of work."""
    runtime = planning_runtime(tmp_path, "branch.db")
    register_branching(runtime)
    work = make_branching_work(runtime)
    runtime.event_store.append(
        Event("work_required", "test", {"work_requirement_id": str(work.id)})
    )
    runtime.close()

    for _ in range(80):
        runtime = rebuild(tmp_path)
        runtime.dispatch_pending_events(limit=1)
        instance = runtime.scheduler.next_runnable()
        if instance is not None:
            await runtime.executor.execute(instance)
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

    plan = only_plan(runtime)
    assert plan.status is PlanStatus.COMPLETED
    assert set(node_statuses(runtime, plan).values()) == {"COMPLETED"}
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    for name in BRANCH_NODES:
        assert len(instances_named(runtime, name)) == 1
    runtime.close()
