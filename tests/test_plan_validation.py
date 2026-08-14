"""AT2, AT4, AT20, AT21, AT22 (spec §97, §99, §115–§117): nothing runs unvalidated.

The proposal boundary again (spec §69): the component that composes a plan is
not the one that decides it may run.  And because the world moves between those
two moments, the check happens twice — at composition, and again just before
each stage spawns.
"""

from __future__ import annotations

from planning_helpers import (
    PlanStatus,
    make_work,
    offer_work,
    only_plan,
    planning_runtime,
    register_chain,
    register_step,
    status_of,
)

from nexus_seed.core.event import Event
from nexus_seed.planning.models import (
    BindingStatus,
    PlanCandidate,
    PlanEdge,
    PlanNode,
    SearchBounds,
)
from nexus_seed.planning.validation import PlanValidator
from nexus_seed.work.work_requirement import WorkStatus

import uuid


def node(key, inputs=(), outputs=(), provides=(), depth=0, plan_id=None):
    return PlanNode(
        plan_id=plan_id or uuid.uuid4(),
        node_key=key,
        definition_name=key.split(":")[0],
        definition_version="1",
        provided_capabilities=list(provides),
        input_types=list(inputs),
        output_types=list(outputs),
        depth=depth,
    )


async def test_a_cycle_is_refused(tmp_path):
    """AT2: a loop belongs inside a process, not in the plan graph."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("x",), ("y",))
    register_step(runtime, "p2", "b", ("y",), ("x",))

    plan_id = uuid.uuid4()
    n1 = node("p1:v1", ("x",), ("y",), ("a",), plan_id=plan_id)
    n2 = node("p2:v1", ("y",), ("x",), ("b",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[n1, n2],
        edges=[
            PlanEdge(plan_id=plan_id, from_node_id=n1.id, to_node_id=n2.id, artifact_type="y"),
            PlanEdge(plan_id=plan_id, from_node_id=n2.id, to_node_id=n1.id, artifact_type="x"),
        ],
    )

    validation = PlanValidator(runtime.capabilities).validate_candidate(
        candidate, runtime.process_store.all_definitions()
    )

    assert not validation.ok
    assert "plan contains a cycle" in validation.reasons
    runtime.close()


async def test_a_chain_is_not_a_cycle(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("x",), ("y",))
    register_step(runtime, "p2", "b", ("y",), ("z",))

    plan_id = uuid.uuid4()
    n1 = node("p1:v1", ("x",), ("y",), ("a",), plan_id=plan_id)
    n2 = node("p2:v1", ("y",), ("z",), ("b",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[n1, n2],
        edges=[
            PlanEdge(plan_id=plan_id, from_node_id=n1.id, to_node_id=n2.id, artifact_type="y")
        ],
        produced_output_types=["y", "z"],
    )

    validation = PlanValidator(runtime.capabilities).validate_candidate(
        candidate,
        runtime.process_store.all_definitions(),
        available_input_types=["x"],
        required_output_types=["z"],
    )
    assert validation.ok, validation.reasons
    runtime.close()


async def test_unreachable_inputs_are_refused(tmp_path):
    """AT4: capability coverage is not enough if the data cannot flow."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("geometry",), ("report",))

    candidate = PlanCandidate(
        nodes=[node("p1:v1", ("geometry",), ("report",), ("a",))],
        produced_output_types=["report"],
    )
    validation = PlanValidator(runtime.capabilities).validate_candidate(
        candidate,
        runtime.process_store.all_definitions(),
        available_input_types=["measurement"],
        required_output_types=["report"],
    )

    assert not validation.ok
    assert any("nothing supplies this input" in r for r in validation.reasons)
    assert validation.has(BindingStatus.MISSING_INPUT)
    runtime.close()


async def test_missing_required_output_is_refused(tmp_path):
    """AT20: every node ran is not the same as the job being done."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("x",), ("y",))

    candidate = PlanCandidate(
        nodes=[node("p1:v1", ("x",), ("y",), ("a",))],
        produced_output_types=["y"],
    )
    validation = PlanValidator(runtime.capabilities).validate_candidate(
        candidate,
        runtime.process_store.all_definitions(),
        available_input_types=["x"],
        required_output_types=["analysis_report"],
    )

    assert not validation.ok
    assert any("required outputs not produced" in r for r in validation.reasons)
    runtime.close()


async def test_an_empty_plan_is_refused(tmp_path):
    runtime = planning_runtime(tmp_path)
    validation = PlanValidator(runtime.capabilities).validate_candidate(
        PlanCandidate(), []
    )
    assert not validation.ok
    assert "plan has no nodes" in validation.reasons
    runtime.close()


async def test_a_plan_over_the_node_budget_is_refused(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", (), ("y",))
    candidate = PlanCandidate(nodes=[node("p1:v1", (), ("y",), ("a",)) for _ in range(3)])

    validation = PlanValidator(
        runtime.capabilities, bounds=SearchBounds(max_plan_nodes=2)
    ).validate_candidate(candidate, runtime.process_store.all_definitions())

    assert not validation.ok
    assert any("over the limit" in r for r in validation.reasons)
    runtime.close()


async def test_a_node_naming_an_unknown_definition_is_refused(tmp_path):
    runtime = planning_runtime(tmp_path)
    candidate = PlanCandidate(nodes=[node("ghost:v1", (), ("y",), ("a",))])

    validation = PlanValidator(runtime.capabilities).validate_candidate(candidate, [])

    assert not validation.ok
    assert any("no such ProcessDefinition" in r for r in validation.reasons)
    runtime.close()


# --- the second check, just before execution -------------------------------


async def test_disabling_a_definition_blocks_the_plan_before_it_runs(tmp_path):
    """AT21: the world moved between planning and running."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    work = make_work(runtime)

    # Compose without executing: no plan executor registered yet.
    runtime.registry._handlers.pop("execute_process_plan", None)
    await offer_work(runtime, work)
    plan = only_plan(runtime)
    assert plan.status is PlanStatus.VALIDATED

    # The middle step is withdrawn.
    runtime.set_capability_enabled("analyze_resistance", "1", False)
    from nexus_seed.processes.planning import EXECUTE_PROCESS_PLAN, execute_process_plan

    runtime.register_process(EXECUTE_PROCESS_PLAN, execute_process_plan)
    await runtime.submit_event(
        Event("process_plan_created", "test", {"plan_id": str(plan.id)})
    )

    # The first node was fine, so it ran; the second stage was refused.
    refreshed = runtime.get_plan(plan.id)
    assert refreshed.status is PlanStatus.BLOCKED
    assert status_of(runtime, work) is not WorkStatus.SATISFIED
    runtime.close()


async def test_registry_changes_do_not_rewrite_an_existing_plan(tmp_path):
    """AT22: a plan is a decision already made (Invariant 57)."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    plan = only_plan(runtime)
    before = [n.node_key for n in runtime.get_plan_nodes(plan.id)]

    register_step(runtime, "better_analyzer", "analyze_resistance", ("measurement",), ("resistance_analysis",), priority=99)

    after = [n.node_key for n in runtime.get_plan_nodes(plan.id)]
    assert after == before
    runtime.close()
