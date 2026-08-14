"""AT1, AT13, AT23 (spec §96, §108, §118): the plan as a durable record.

A composed plan is a temporary organisation the system invented for one job.
The trace is what makes that answerable afterwards: who took which part, what
they were given, what they produced, and why this arrangement.
"""

from __future__ import annotations

import uuid

from planning_helpers import (
    PlanStatus,
    instances_named,
    make_work,
    offer_work,
    only_plan,
    planning_runtime,
    register_chain,
    register_step,
    wire,
)

from nexus_seed.runtime.runtime import Runtime


async def test_a_trace_shows_the_whole_arrangement(tmp_path):
    """AT23."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)
    await offer_work(runtime, work)

    trace = runtime.get_plan_trace(only_plan(runtime).id)

    assert trace.status == "COMPLETED"
    assert trace.order == [
        "extract_measurement:v1",
        "analyze_resistance:v1",
        "generate_analysis_report:v1",
    ]
    assert set(trace.node_statuses.values()) == {"COMPLETED"}
    assert len(trace.instances) == 3
    assert trace.requirement.id == work.id
    assert trace.reasons  # why this shape
    runtime.close()


async def test_a_trace_reaches_each_node_output(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    outputs = runtime.get_plan_trace(only_plan(runtime).id).outputs

    assert outputs["extract_measurement:v1"]["outputs"][0]["type"] == "measurement"
    assert outputs["generate_analysis_report:v1"]["outputs"][0]["type"] == "analysis_report"
    runtime.close()


async def test_a_trace_connects_back_to_capability_matching(tmp_path):
    """The chain: need -> COMPOSITION_REQUIRED -> plan -> processes."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)
    await offer_work(runtime, work)

    trace = runtime.get_plan_trace(only_plan(runtime).id)

    assert [m.status.value for m in trace.capability_matches] == ["COMPOSITION_REQUIRED"]
    assert trace.capability_matches[0].work_requirement_id == work.id
    # And the capability trace names the same work.
    capability_trace = runtime.get_capability_trace(work.id)
    assert capability_trace.was_blocked
    runtime.close()


async def test_the_planning_snapshot_records_what_was_chosen_from(tmp_path):
    """Spec §64-§65: what the decision rested on, not the whole registry."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    register_step(runtime, "unrelated", "something_else", ("x",), ("y",))
    await offer_work(runtime, make_work(runtime))

    snapshot = only_plan(runtime).planning_snapshot

    chosen = {d["name"] for d in snapshot["definitions"]}
    assert chosen == {"extract_measurement", "analyze_resistance", "generate_analysis_report"}
    assert "unrelated" not in chosen
    assert snapshot["definitions"][0]["outputs"] == ["measurement"]
    assert snapshot["candidates"]
    runtime.close()


async def test_the_plan_structure_survives_a_restart(tmp_path):
    """AT1."""
    runtime = planning_runtime(tmp_path, "trace.db")
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))
    plan_id = only_plan(runtime).id
    before = runtime.get_plan_trace(plan_id)
    runtime.close()

    runtime2 = wire(Runtime(tmp_path / "trace.db"))
    after = runtime2.get_plan_trace(plan_id)

    assert after.order == before.order
    assert after.node_statuses == before.node_statuses
    assert after.edge_pairs == before.edge_pairs
    assert after.plan.status is PlanStatus.COMPLETED
    assert after.plan.planning_snapshot == before.plan.planning_snapshot
    runtime2.close()


async def test_each_instance_knows_which_plan_position_it_filled(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))
    plan = only_plan(runtime)

    instance = instances_named(runtime, "analyze_resistance")[0]
    assert instance.plan_id == plan.id
    node = runtime.plan_store.get_node(instance.plan_node_id)
    assert node.node_key == "analyze_resistance:v1"
    assert node.process_instance_id == instance.id
    runtime.close()


async def test_the_active_plan_is_findable_from_the_work(tmp_path):
    """Spec §130."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    runtime.registry._handlers.pop("execute_process_plan", None)
    await offer_work(runtime, work)

    active = runtime.get_active_plan(work.id)
    assert active is not None
    assert active.status is PlanStatus.VALIDATED
    assert [p.id for p in runtime.get_plans_for_work(work.id)] == [active.id]
    runtime.close()


def test_an_unknown_plan_has_no_trace(tmp_path):
    runtime = Runtime(tmp_path / "t.db")
    assert runtime.get_plan_trace(uuid.uuid4()) is None
    assert runtime.get_plan(uuid.uuid4()) is None
    runtime.close()


async def test_plan_events_are_durably_delivered(tmp_path):
    """AT27: a plan created but not yet routed still starts after a restart."""
    from nexus_seed.core.event import Event

    runtime = planning_runtime(tmp_path, "durable.db")
    register_chain(runtime)
    work = make_work(runtime)

    # Compose without an executor registered, so the event is not consumed.
    runtime.registry._handlers.pop("execute_process_plan", None)
    await offer_work(runtime, work)
    plan_id = only_plan(runtime).id

    # The plan-created event exists; nothing acted on it.
    created = runtime.event_store.by_type("process_plan_created")[0]
    assert runtime.get_plan(plan_id).status is PlanStatus.VALIDATED
    runtime.close()

    # --- restart with the executor available; re-offer the stored event ---
    runtime2 = wire(Runtime(tmp_path / "durable.db"))
    register_chain(runtime2)
    await runtime2.submit_event(
        Event("process_plan_created", "recovery", {"plan_id": str(plan_id)})
    )

    assert runtime2.get_plan(plan_id).status is PlanStatus.COMPLETED
    assert runtime2.get_event_delivery(created.id).status.value == "DELIVERED"
    runtime2.close()
