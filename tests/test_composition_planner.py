"""AT3, AT4, AT8, AT9, AT10 (spec §98–§105): the planner itself.

Deterministic backward chaining over exact symbolic types.  The properties that
matter are that it finds the chain when one exists, refuses to invent one when
it does not, and always stops.
"""

from __future__ import annotations

from planning_helpers import CHAIN, declare_capability, planning_runtime, register_step

from nexus_seed.capabilities.models import CapabilityRequirement
from nexus_seed.planning.models import SearchBounds
from nexus_seed.planning.planner import CompositionPlanner


def plan_for(runtime, required, inputs=(), outputs=(), bounds=None):
    planner = CompositionPlanner(runtime.capabilities, bounds=bounds)
    candidates = planner.plan(
        [CapabilityRequirement(name=n) for n in required],
        runtime.process_store.all_definitions(),
        available_input_types=list(inputs),
        required_output_types=list(outputs),
    )
    planner.score_candidates(candidates, runtime.process_store.all_definitions())
    return candidates


def keys(candidate):
    return [n.node_key for n in candidate.nodes]


async def test_a_linear_chain_is_found(tmp_path):
    """AT8."""
    runtime = planning_runtime(tmp_path)
    for name, inputs, outputs in CHAIN:
        register_step(runtime, name, name, inputs, outputs)

    candidates = plan_for(
        runtime,
        [name for name, _, _ in CHAIN],
        inputs=("raw_measurement_resource",),
        outputs=("analysis_report",),
    )

    assert candidates
    best = candidates[0]
    assert keys(best) == [
        "extract_measurement:v1",
        "analyze_resistance:v1",
        "generate_analysis_report:v1",
    ]
    assert best.missing_capabilities == []
    assert best.missing_output_types == []
    assert best.depth == 3
    runtime.close()


async def test_types_must_match_exactly(tmp_path):
    """AT3 + AT4: connection is symbolic equality, nothing more."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("raw",), ("geometry",))
    register_step(runtime, "p2", "b", ("measurement",), ("report",))

    candidates = plan_for(runtime, ["a", "b"], inputs=("raw",), outputs=("report",))

    # p2 needs a measurement; nothing produces one, however similar the names.
    assert candidates == []
    runtime.close()


async def test_a_matching_type_connects(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("raw",), ("measurement",))
    register_step(runtime, "p2", "b", ("measurement",), ("report",))

    best = plan_for(runtime, ["a", "b"], inputs=("raw",), outputs=("report",))[0]

    assert keys(best) == ["p1:v1", "p2:v1"]
    assert [(e.artifact_type) for e in best.edges] == ["measurement"]
    runtime.close()


async def test_an_intermediate_step_is_pulled_in_by_backward_chaining(tmp_path):
    """The work asks for the report; the planner works out it needs the rest."""
    runtime = planning_runtime(tmp_path)
    for name, inputs, outputs in CHAIN:
        register_step(runtime, name, name, inputs, outputs)

    # Only the last capability is required; the others are discovered.
    best = plan_for(
        runtime,
        ["generate_analysis_report"],
        inputs=("raw_measurement_resource",),
        outputs=("analysis_report",),
    )[0]

    assert keys(best) == [
        "extract_measurement:v1",
        "analyze_resistance:v1",
        "generate_analysis_report:v1",
    ]
    runtime.close()


async def test_planning_stops_when_an_input_can_never_be_produced(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("nobody_makes_this",), ("report",))

    assert plan_for(runtime, ["a"], inputs=(), outputs=("report",)) == []
    runtime.close()


async def test_the_search_respects_its_node_budget(tmp_path):
    """AT10: bounded, so a big graph ends in an answer rather than a hang."""
    runtime = planning_runtime(tmp_path)
    # A 6-long chain: t0 -> t1 -> ... -> t6
    for n in range(6):
        register_step(runtime, f"step{n}", f"cap{n}", (f"t{n}",), (f"t{n + 1}",))

    unbounded = plan_for(runtime, ["cap5"], inputs=("t0",), outputs=("t6",))
    assert len(unbounded[0].nodes) == 6

    bounded = plan_for(
        runtime,
        ["cap5"],
        inputs=("t0",),
        outputs=("t6",),
        bounds=SearchBounds(max_plan_nodes=3),
    )
    assert bounded == []
    runtime.close()


async def test_the_search_respects_its_depth_budget(tmp_path):
    runtime = planning_runtime(tmp_path)
    for n in range(6):
        register_step(runtime, f"step{n}", f"cap{n}", (f"t{n}",), (f"t{n + 1}",))

    bounded = plan_for(
        runtime,
        ["cap5"],
        inputs=("t0",),
        outputs=("t6",),
        bounds=SearchBounds(max_search_depth=2),
    )
    assert bounded == []
    runtime.close()


async def test_planning_is_reproducible(tmp_path):
    """AT8: same inputs, same plan, every time."""
    runtime = planning_runtime(tmp_path)
    for name, inputs, outputs in CHAIN:
        register_step(runtime, name, name, inputs, outputs)

    plans = {
        tuple(keys(plan_for(
            runtime,
            [n for n, _, _ in CHAIN],
            inputs=("raw_measurement_resource",),
            outputs=("analysis_report",),
        )[0]))
        for _ in range(5)
    }
    assert len(plans) == 1
    runtime.close()


async def test_planning_survives_a_restart_unchanged(tmp_path):
    from nexus_seed.runtime.runtime import Runtime
    from planning_helpers import wire

    runtime = planning_runtime(tmp_path, "reproducible.db")
    for name, inputs, outputs in CHAIN:
        register_step(runtime, name, name, inputs, outputs)
    first = keys(
        plan_for(
            runtime,
            [n for n, _, _ in CHAIN],
            inputs=("raw_measurement_resource",),
            outputs=("analysis_report",),
        )[0]
    )
    runtime.close()

    runtime2 = wire(Runtime(tmp_path / "reproducible.db"))
    second = keys(
        plan_for(
            runtime2,
            [n for n, _, _ in CHAIN],
            inputs=("raw_measurement_resource",),
            outputs=("analysis_report",),
        )[0]
    )
    assert second == first
    runtime2.close()


async def test_the_planner_has_no_side_effects(tmp_path):
    """Spec §132: it computes candidates and nothing else."""
    runtime = planning_runtime(tmp_path)
    for name, inputs, outputs in CHAIN:
        register_step(runtime, name, name, inputs, outputs)

    instances_before = len(runtime.process_store.all_instances())
    events_before = len(runtime.event_store.all())

    plan_for(
        runtime,
        [n for n, _, _ in CHAIN],
        inputs=("raw_measurement_resource",),
        outputs=("analysis_report",),
    )

    # Registration announced capabilities; planning added nothing at all.
    assert runtime.get_plans() == []
    assert len(runtime.process_store.all_instances()) == instances_before
    assert len(runtime.event_store.all()) == events_before
    runtime.close()


async def test_a_disabled_definition_is_not_composed_in(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("raw",), ("measurement",))
    register_step(runtime, "p2", "b", ("measurement",), ("report",), enabled=False)

    assert plan_for(runtime, ["a", "b"], inputs=("raw",), outputs=("report",)) == []
    runtime.close()


async def test_a_disabled_capability_is_not_composed_in(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "p1", "a", ("raw",), ("measurement",))
    register_step(runtime, "p2", "b", ("measurement",), ("report",))
    runtime.set_capability_enabled("b", "1", False)

    assert plan_for(runtime, ["a", "b"], inputs=("raw",), outputs=("report",)) == []
    runtime.close()


async def test_no_llm_is_consulted(tmp_path):
    """Invariant 64: composition is exact, never inferred."""
    runtime = planning_runtime(tmp_path)
    for name, inputs, outputs in CHAIN:
        register_step(runtime, name, name, inputs, outputs)

    class ExplodingBackend:
        async def execute(self, request):  # pragma: no cover - must not be called
            raise AssertionError("the planner must not call an LLM")

    runtime.register_backend("llm", ExplodingBackend())
    candidates = plan_for(
        runtime,
        [n for n, _, _ in CHAIN],
        inputs=("raw_measurement_resource",),
        outputs=("analysis_report",),
    )
    assert candidates
    runtime.close()
