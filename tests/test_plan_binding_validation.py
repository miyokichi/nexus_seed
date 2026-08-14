"""AT7–AT13 (spec §11–§20): an unclear plan is refused, not guessed at.

The rule this phase adds to the proposal boundary: *ambiguity is a validation
failure* (Invariant 68).  A planner that quietly picks one of two possible
producers makes a decision nobody can review and nobody can reproduce — the
system would still run, which is what makes it dangerous.
"""

from __future__ import annotations

import uuid

from planning_helpers import (
    make_work,
    offer_work,
    planning_runtime,
    register_step,
    status_of,
)

from nexus_seed.planning.models import (
    BindingStatus,
    PlanCandidate,
    PlanEdge,
    PlanNode,
)
from nexus_seed.planning.validation import PlanValidator
from nexus_seed.work.work_requirement import WorkStatus


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


def validate(runtime, candidate, *, available=(), required=()):
    return PlanValidator(runtime.capabilities).validate_candidate(
        candidate,
        runtime.process_store.all_definitions(),
        available_input_types=list(available),
        required_output_types=list(required),
    )


# --- ambiguity -------------------------------------------------------------


async def test_two_producers_of_one_type_are_refused_not_ranked(tmp_path):
    """AT7: the headline case."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "extract_a", "extract_a", ("raw",), ("measurement",))
    register_step(runtime, "extract_b", "extract_b", ("raw",), ("measurement",))
    register_step(runtime, "analyze", "analyze", ("measurement",), ("report",))
    work = make_work(
        runtime,
        required=("extract_a", "extract_b", "analyze"),
        inputs=("raw",),
        outputs=("report",),
    )

    await offer_work(runtime, work)

    # No plan was committed, and the need still stands (Invariant 63).
    assert runtime.get_plans() == []
    # Providers exist, but no unambiguous binding can be planned.  This is a
    # plan gap, not a capability gap (Phase 4C).
    assert status_of(runtime, work) is WorkStatus.BLOCKED_PLAN
    runtime.close()


async def test_the_refusal_names_the_input_and_both_candidates(tmp_path):
    """AT8: 'it did not work' is not an answer anyone can act on."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "extract_a", "extract_a", ("raw",), ("measurement",))
    register_step(runtime, "extract_b", "extract_b", ("raw",), ("measurement",))
    register_step(runtime, "analyze", "analyze", ("measurement",), ("report",))
    work = make_work(
        runtime,
        required=("extract_a", "extract_b", "analyze"),
        inputs=("raw",),
        outputs=("report",),
    )

    await offer_work(runtime, work)

    reasons = runtime.event_store.by_type("process_plan_unavailable")[0].payload["reasons"]
    ambiguity = [r for r in reasons if "analyze:v1.measurement" in r]
    assert ambiguity, reasons
    assert "extract_a:v1" in ambiguity[0] and "extract_b:v1" in ambiguity[0]
    runtime.close()


async def test_the_validator_reports_ambiguity_as_ambiguity(tmp_path):
    """AT9: not as 'nothing supplies this' — the difference is the diagnosis."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m",))
    register_step(runtime, "b", "b", (), ("m",))
    register_step(runtime, "c", "c", ("m",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m",), ("a",), plan_id=plan_id)
    b = node("b:v1", (), ("m",), ("b",), plan_id=plan_id)
    c = node("c:v1", ("m",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, b, c],
        produced_output_types=["m", "out"],
        ambiguous_bindings=[
            {"node": "c:v1", "port": "m", "sources": ["a:v1", "b:v1"]}
        ],
    )

    validation = validate(runtime, candidate, required=["out"])

    assert not validation.ok
    assert validation.has(BindingStatus.AMBIGUOUS_BINDING)
    assert not validation.has(BindingStatus.MISSING_INPUT)
    runtime.close()


# --- duplicates, mismatches, unknown ports ---------------------------------


async def test_one_input_fed_by_two_edges_is_refused(tmp_path):
    """AT10: fan-in aggregation is not something this phase defines (spec §20)."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m",))
    register_step(runtime, "b", "b", (), ("m",))
    register_step(runtime, "c", "c", ("m",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m",), ("a",), plan_id=plan_id)
    b = node("b:v1", (), ("m",), ("b",), plan_id=plan_id)
    c = node("c:v1", ("m",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, b, c],
        edges=[
            PlanEdge(plan_id, a.id, c.id, output_type="m", input_type="m"),
            PlanEdge(plan_id, b.id, c.id, output_type="m", input_type="m"),
        ],
        produced_output_types=["m", "out"],
    )

    validation = validate(runtime, candidate, required=["out"])

    assert not validation.ok
    assert validation.has(BindingStatus.DUPLICATE_BINDING)
    assert any("2 producers" in r for r in validation.reasons)
    runtime.close()


async def test_an_edge_whose_ends_are_different_types_is_refused(tmp_path):
    """AT11: no conversion, no coercion (Invariant 59)."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m",))
    register_step(runtime, "c", "c", ("n",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m",), ("a",), plan_id=plan_id)
    c = node("c:v1", ("n",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, c],
        edges=[PlanEdge(plan_id, a.id, c.id, output_type="m", input_type="n")],
        produced_output_types=["m", "out"],
    )

    validation = validate(runtime, candidate, required=["out"])

    assert not validation.ok
    assert validation.has(BindingStatus.TYPE_MISMATCH)
    runtime.close()


async def test_an_edge_drawing_from_a_port_the_producer_lacks_is_refused(tmp_path):
    """AT12: a binding to nothing is worse than no binding."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m:left",))
    register_step(runtime, "c", "c", ("m",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m:left",), ("a",), plan_id=plan_id)
    c = node("c:v1", ("m",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, c],
        edges=[
            PlanEdge(
                plan_id, a.id, c.id,
                output_type="m", output_key="ghost", input_type="m",
            )
        ],
        produced_output_types=["m", "out"],
    )

    validation = validate(runtime, candidate, required=["out"])

    assert not validation.ok
    assert validation.has(BindingStatus.INVALID_PORT)
    assert any("does not produce" in r for r in validation.reasons)
    runtime.close()


async def test_an_edge_feeding_a_port_the_consumer_lacks_is_refused(tmp_path):
    """The other end of AT12."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m",))
    register_step(runtime, "c", "c", ("m:right",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m",), ("a",), plan_id=plan_id)
    c = node("c:v1", ("m:right",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, c],
        edges=[
            PlanEdge(
                plan_id, a.id, c.id,
                output_type="m", input_type="m", input_key="ghost",
            )
        ],
        produced_output_types=["m", "out"],
    )

    validation = validate(runtime, candidate, required=["out"])

    assert not validation.ok
    assert validation.has(BindingStatus.INVALID_PORT)
    assert any("does not consume" in r for r in validation.reasons)
    runtime.close()


async def test_a_keyless_producer_may_be_drawn_from_under_a_key(tmp_path):
    """Spec §10: on the producing side a bare type is a wildcard.

    Which is the same rule that makes a keyless *consumer* facing two producers
    ambiguous — seen from the other end.
    """
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m",))
    register_step(runtime, "c", "c", ("m:wanted",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m",), ("a",), plan_id=plan_id)
    c = node("c:v1", ("m:wanted",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, c],
        edges=[
            PlanEdge(
                plan_id, a.id, c.id,
                output_type="m", output_key="wanted",
                input_type="m", input_key="wanted",
            )
        ],
        produced_output_types=["m", "out"],
    )

    assert validate(runtime, candidate, required=["out"]).ok
    runtime.close()


async def test_the_consuming_side_is_not_a_wildcard(tmp_path):
    """An edge may not invent a name the consumer never declared.

    The input port's name is the dict key the handler is given.  A wildcard
    here would hand a process a value under a name it does not read — at run
    time, quietly, with the plan reporting success.
    """
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m",))
    register_step(runtime, "c", "c", ("m",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m",), ("a",), plan_id=plan_id)
    c = node("c:v1", ("m",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, c],
        edges=[
            PlanEdge(
                plan_id, a.id, c.id,
                output_type="m", input_type="m", input_key="invented",
            )
        ],
        produced_output_types=["m", "out"],
    )

    validation = validate(runtime, candidate, required=["out"])

    assert not validation.ok
    assert validation.has(BindingStatus.INVALID_PORT)
    runtime.close()


async def test_a_key_that_no_producer_offers_leaves_the_input_unsupplied(tmp_path):
    """AT13: a key is part of the type check, not decoration."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m:left",))
    register_step(runtime, "c", "c", ("m:right",), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", (), ("m:left",), ("a",), plan_id=plan_id)
    c = node("c:v1", ("m:right",), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(nodes=[a, c], produced_output_types=["m", "out"])

    validation = validate(runtime, candidate, required=["out"])

    assert not validation.ok
    assert validation.has(BindingStatus.MISSING_INPUT)
    assert any("c:v1.m:right" in r for r in validation.reasons)
    runtime.close()


async def test_a_fully_bound_plan_passes(tmp_path):
    """The control: nothing above fires on a plan that is actually well formed."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", ("raw",), ("m:left",))
    register_step(runtime, "b", "b", ("raw",), ("m:right",))
    register_step(runtime, "c", "c", ("m:left", "m:right"), ("out",))

    plan_id = uuid.uuid4()
    a = node("a:v1", ("raw",), ("m:left",), ("a",), plan_id=plan_id)
    b = node("b:v1", ("raw",), ("m:right",), ("b",), plan_id=plan_id)
    c = node("c:v1", ("m:left", "m:right"), ("out",), ("c",), depth=1, plan_id=plan_id)
    candidate = PlanCandidate(
        nodes=[a, b, c],
        edges=[
            PlanEdge(
                plan_id, a.id, c.id,
                output_type="m", output_key="left",
                input_type="m", input_key="left",
            ),
            PlanEdge(
                plan_id, b.id, c.id,
                output_type="m", output_key="right",
                input_type="m", input_key="right",
            ),
        ],
        produced_output_types=["m", "out"],
    )

    validation = validate(runtime, candidate, available=["raw"], required=["out"])

    assert validation.ok, validation.reasons
    assert validation.binding_issues == []
    runtime.close()


async def test_the_database_refuses_a_second_producer_for_one_input(tmp_path):
    """Invariant 67 is enforced by the schema, not only by the validator."""
    import sqlite3

    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", (), ("m",))
    register_step(runtime, "c", "c", ("m",), ("out",))
    await offer_work(
        runtime, make_work(runtime, required=("a", "c"), inputs=(), outputs=("out",))
    )
    plan = runtime.get_plans()[0]
    nodes = {n.node_key: n for n in runtime.get_plan_nodes(plan.id)}

    duplicate = PlanEdge(
        plan_id=plan.id,
        from_node_id=nodes["a:v1"].id,
        to_node_id=nodes["c:v1"].id,
        output_type="m",
        input_type="m",
    )
    try:
        runtime.plan_store.save_edge(duplicate)
        raise AssertionError("the unique index should have rejected this edge")
    except sqlite3.IntegrityError:
        pass
    runtime.close()
