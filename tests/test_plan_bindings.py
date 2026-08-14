"""AT1–AT6 (spec §9–§14): an edge says which output feeds which input.

Phase 4B connected processes by type and resolved the value at run time, so a
plan with two producers of the same type consumed whichever the executor
happened to visit first.  The plan could not explain its own data flow, and two
runs of the same plan could differ.

Since Phase 4B.1 the binding is the edge (Invariant 66).  Nothing is decided at
execution time that was not decided at planning time.
"""

from __future__ import annotations

from planning_helpers import (
    make_work,
    offer_work,
    only_plan,
    plan_edges,
    planning_runtime,
    received_by,
    register_chain,
    register_step,
    status_of,
)

from nexus_seed.planning.models import Port, TypedOutput, parse_ports, typed_outputs_of
from nexus_seed.work.work_requirement import WorkStatus


# --- the Port model --------------------------------------------------------


def test_a_bare_type_is_a_keyless_port():
    port = Port.parse("measurement")
    assert port.type == "measurement"
    assert port.key is None
    assert port.name == "measurement"
    assert str(port) == "measurement"


def test_a_key_names_one_of_several_same_typed_ports():
    port = Port.parse("measurement:measured")
    assert (port.type, port.key) == ("measurement", "measured")
    assert port.name == "measured"
    assert str(port) == "measurement:measured"


def test_parsing_a_port_is_idempotent():
    port = Port("measurement", "measured")
    assert Port.parse(port) is port
    assert parse_ports(["a", "b:c"]) == [Port("a"), Port("b", "c")]
    assert parse_ports(None) == []


def test_types_must_be_equal_with_no_conversion():
    """Invariant 59: symbolic equality, not compatibility."""
    assert Port("measurement").matches(Port("measurement"))
    assert not Port("measurement").matches(Port("Measurement"))
    assert not Port("measurement").matches(Port("measurement_v2"))


def test_a_keyless_port_accepts_any_key_and_that_is_why_it_is_ambiguous():
    """Spec §10: a wildcard is not a decision."""
    keyless = Port("measurement")
    assert keyless.matches(Port("measurement", "measured"))
    assert keyless.matches(Port("measurement", "reference"))
    # Two producers both match, which is precisely the case the planner refuses.


def test_keys_disagreeing_means_no_match():
    assert not Port("measurement", "measured").matches(Port("measurement", "reference"))
    assert Port("measurement", "measured").matches(Port("measurement", "measured"))


# --- typed outputs ---------------------------------------------------------


def test_a_typed_output_can_carry_a_key():
    output = TypedOutput(type="measurement", value=1, key="measured")
    assert output.port == Port("measurement", "measured")
    assert output.to_dict() == {"type": "measurement", "value": 1, "key": "measured"}
    assert TypedOutput.from_dict(output.to_dict()) == output


def test_a_keyless_typed_output_serializes_without_a_key():
    assert TypedOutput(type="measurement", value=1).to_dict() == {
        "type": "measurement",
        "value": 1,
    }


def test_two_outputs_of_one_type_stay_distinguishable():
    outputs = typed_outputs_of(
        {
            "outputs": [
                {"type": "measurement", "key": "measured", "value": 1},
                {"type": "measurement", "key": "reference", "value": 2},
            ]
        }
    )
    assert [o.port.name for o in outputs] == ["measured", "reference"]


def test_an_untyped_output_contributes_nothing_to_data_flow():
    """Processes that predate typed outputs still run; they just cannot feed."""
    assert typed_outputs_of({"anything": 1}) == []
    assert typed_outputs_of(None) == []


# --- edges as bindings -----------------------------------------------------


async def test_a_composed_edge_names_both_ports(tmp_path):
    """AT1."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    edges = plan_edges(runtime, only_plan(runtime))

    assert [e.describe() for e in edges] == [
        "measurement -> measurement",
        "resistance_analysis -> resistance_analysis",
    ]
    assert all(e.is_bound for e in edges)
    runtime.close()


async def test_bindings_survive_the_round_trip_to_storage(tmp_path):
    """AT5: a binding is durable, not recomputed on read."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "measure", "measure", ("raw",), ("measurement:measured",))
    register_step(
        runtime, "consume", "consume", ("measurement:measured",), ("report",)
    )
    await offer_work(
        runtime,
        make_work(
            runtime,
            required=("measure", "consume"),
            inputs=("raw",),
            outputs=("report",),
        ),
    )

    edge = plan_edges(runtime, only_plan(runtime))[0]

    assert (edge.output_type, edge.output_key) == ("measurement", "measured")
    assert (edge.input_type, edge.input_key) == ("measurement", "measured")
    runtime.close()


async def test_a_value_arrives_under_the_name_its_port_gives_it(tmp_path):
    """AT3: the consumer sees ``measured``, not ``measurement``."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "measure", "measure", ("raw",), ("measurement:measured",))
    register_step(
        runtime, "consume", "consume", ("measurement:measured",), ("report",)
    )
    await offer_work(
        runtime,
        make_work(
            runtime,
            required=("measure", "consume"),
            inputs=("raw",),
            outputs=("report",),
        ),
    )

    assert list(received_by("consume:v1")) == ["measured"]
    runtime.close()


async def test_two_same_typed_inputs_are_told_apart_by_key(tmp_path):
    """AT2: the case Phase 4B could not express at all."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "measure_it", "measure_it", ("raw",), ("measurement:measured",))
    register_step(
        runtime, "look_it_up", "look_it_up", ("raw",), ("measurement:reference",)
    )
    register_step(
        runtime,
        "compare",
        "compare",
        ("measurement:measured", "measurement:reference"),
        ("comparison",),
    )
    work = make_work(
        runtime,
        required=("measure_it", "look_it_up", "compare"),
        inputs=("raw",),
        outputs=("comparison",),
    )
    await offer_work(runtime, work)

    received = received_by("compare:v1")
    assert received == {
        "measured": "measured-from-measure_it",
        "reference": "reference-from-look_it_up",
    }
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_the_same_plan_binds_the_same_way_every_time(tmp_path):
    """AT6: determinism (Invariant 64) now covers data flow, not just order."""
    shapes = []
    for run in range(3):
        runtime = planning_runtime(tmp_path, f"determinism{run}.db")
        register_step(runtime, "a", "a", ("raw",), ("measurement:measured",))
        register_step(runtime, "b", "b", ("raw",), ("measurement:reference",))
        register_step(
            runtime,
            "c",
            "c",
            ("measurement:measured", "measurement:reference"),
            ("out",),
        )
        await offer_work(
            runtime,
            make_work(
                runtime,
                required=("a", "b", "c"),
                inputs=("raw",),
                outputs=("out",),
            ),
        )
        shapes.append(sorted(e.describe() for e in plan_edges(runtime, only_plan(runtime))))
        runtime.close()

    assert shapes[0] == shapes[1] == shapes[2]
