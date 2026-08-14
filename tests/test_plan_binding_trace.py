"""AT29–AT33 (spec §23–§24): where did this value come from?

Phase 4B's trace could say which node ran before which.  That answers "in what
order", not "why did this process see this number" — and the second question is
the one anybody debugging a composed plan actually has.  Bindings are explicit
now, so the trace can answer it down to the port.
"""

from __future__ import annotations

from planning_helpers import (
    make_branching_work,
    make_work,
    offer_work,
    only_plan,
    planning_runtime,
    register_branching,
    register_chain,
    register_step,
    wire,
)

from nexus_seed.planning.trace import PlanBinding
from nexus_seed.runtime.runtime import Runtime


async def test_the_trace_names_producer_and_consumer_ports(tmp_path):
    """AT29."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    bindings = runtime.get_plan_trace(only_plan(runtime).id).bindings

    assert [b.describe() for b in bindings] == [
        "extract_measurement:v1.measurement -> analyze_resistance:v1.measurement",
        "analyze_resistance:v1.resistance_analysis -> "
        "generate_analysis_report:v1.resistance_analysis",
    ]
    assert all(isinstance(b, PlanBinding) and b.bound for b in bindings)
    runtime.close()


async def test_a_keyed_binding_shows_its_key(tmp_path):
    """AT30: the key is the whole reason two same-typed values stay apart."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "a", "a", ("raw",), ("m:left",))
    register_step(runtime, "b", "b", ("raw",), ("m:right",))
    register_step(runtime, "c", "c", ("m:left", "m:right"), ("out",))
    await offer_work(
        runtime,
        make_work(
            runtime, required=("a", "b", "c"), inputs=("raw",), outputs=("out",)
        ),
    )

    trace = runtime.get_plan_trace(only_plan(runtime).id)
    into_c = {b.producer_node_key: b for b in trace.bindings_into("c:v1")}

    assert into_c["a:v1"].consumer_port == "m:left"
    assert into_c["a:v1"].input_name == "left"
    assert into_c["b:v1"].consumer_port == "m:right"
    assert into_c["b:v1"].input_name == "right"
    runtime.close()


async def test_the_trace_shows_what_each_position_was_actually_given(tmp_path):
    """AT31: read back from the instances, not recomputed from the plan.

    A binding says what *should* have been passed.  ``inputs_given`` says what
    was — and the two agreeing is the thing worth checking.
    """
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    trace = runtime.get_plan_trace(only_plan(runtime).id)
    given = trace.inputs_given

    assert given["generate_analysis_report:v1"] == {
        "resistance_analysis": "resistance_analysis-from-analyze_resistance",
        "thermal_analysis": "thermal_analysis-from-analyze_thermal",
    }
    # Every binding into that node named an input the node really received.
    for binding in trace.bindings_into("generate_analysis_report:v1"):
        assert binding.input_name in given["generate_analysis_report:v1"]
    runtime.close()


async def test_fan_out_appears_as_two_bindings_from_one_port(tmp_path):
    """AT32: the diamond, read as data flow rather than as order."""
    runtime = planning_runtime(tmp_path)
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))

    trace = runtime.get_plan_trace(only_plan(runtime).id)

    assert sorted(trace.binding_pairs) == [
        (
            "analyze_resistance:v1", "resistance_analysis",
            "generate_analysis_report:v1", "resistance_analysis",
        ),
        (
            "analyze_thermal:v1", "thermal_analysis",
            "generate_analysis_report:v1", "thermal_analysis",
        ),
        ("extract_measurement:v1", "measurement", "analyze_resistance:v1", "measurement"),
        ("extract_measurement:v1", "measurement", "analyze_thermal:v1", "measurement"),
    ]
    runtime.close()


async def test_binding_provenance_survives_a_restart(tmp_path):
    """AT33: the trace is read from storage, not rebuilt by the planner."""
    runtime = planning_runtime(tmp_path, "prov.db")
    register_branching(runtime)
    await offer_work(runtime, make_branching_work(runtime))
    plan_id = only_plan(runtime).id
    before = runtime.get_plan_trace(plan_id)
    before_pairs = sorted(before.binding_pairs)
    before_given = before.inputs_given
    runtime.close()

    runtime2 = wire(Runtime(tmp_path / "prov.db"))
    after = runtime2.get_plan_trace(plan_id)

    assert sorted(after.binding_pairs) == before_pairs
    assert after.inputs_given == before_given
    runtime2.close()


async def test_the_planning_audit_records_the_bindings_it_chose(tmp_path):
    """The snapshot explains the shape, so it must include the data flow."""
    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    candidates = only_plan(runtime).planning_snapshot["candidates"]
    chosen = candidates[0]

    assert [e["binding"] for e in chosen["edges"]] == [
        "measurement -> measurement",
        "resistance_analysis -> resistance_analysis",
    ]
    runtime.close()
