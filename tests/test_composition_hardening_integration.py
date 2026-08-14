"""AT66–AT72 (spec §60–§64): the whole of Phase 4B.1, end to end.

One requirement no single process can meet, satisfied by a branching plan whose
data flow is explicit, run in slices the caller controls.  Each earlier file
holds one claim still; this one checks they hold together, because that is
where a hardening phase usually turns out not to have hardened anything.

Nothing here is new behaviour.  What is new is that all of it is true at once.
"""

from __future__ import annotations

from drain_helpers import DrainBudget
from planning_helpers import (
    RECORDED,
    PlanStatus,
    WorkStatus,
    instances_named,
    make_work,
    node_statuses,
    offer_work,
    only_plan,
    plan_edges,
    planning_runtime,
    received_by,
    register_step,
    status_of,
    work_required,
)

#: A shape that exercises every part of the phase at once: a fan-out, two
#: same-typed results told apart by key, and a join.
def register_lab(runtime):
    register_step(
        runtime, "load_sample", "load_sample", ("raw_resource",), ("sample",)
    )
    register_step(
        runtime, "measure", "measure", ("sample",), ("reading:measured",)
    )
    register_step(
        runtime, "lookup_reference", "lookup_reference", ("sample",), ("reading:reference",)
    )
    register_step(
        runtime,
        "compare",
        "compare",
        ("reading:measured", "reading:reference"),
        ("comparison",),
    )
    register_step(
        runtime, "write_report", "write_report", ("comparison",), ("analysis_report",)
    )


LAB_CAPABILITIES = (
    "load_sample",
    "measure",
    "lookup_reference",
    "compare",
    "write_report",
)


def lab_work(runtime, **kwargs):
    kwargs.setdefault("work_key", "lab")
    kwargs.setdefault("required", LAB_CAPABILITIES)
    kwargs.setdefault("inputs", ("raw_resource",))
    kwargs.setdefault("outputs", ("analysis_report",))
    return make_work(runtime, **kwargs)


async def test_the_whole_arrangement_runs_and_satisfies_the_work(tmp_path):
    """AT66."""
    runtime = planning_runtime(tmp_path)
    register_lab(runtime)
    work = lab_work(runtime)

    await offer_work(runtime, work)

    plan = only_plan(runtime)
    assert plan.status is PlanStatus.COMPLETED
    assert set(node_statuses(runtime, plan).values()) == {"COMPLETED"}
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_the_two_same_typed_readings_never_get_mixed_up(tmp_path):
    """AT67: the case that motivated the whole phase."""
    runtime = planning_runtime(tmp_path)
    register_lab(runtime)
    await offer_work(runtime, lab_work(runtime))

    assert received_by("compare:v1") == {
        "measured": "measured-from-measure",
        "reference": "reference-from-lookup_reference",
    }
    runtime.close()


async def test_every_edge_in_the_plan_is_explicitly_bound(tmp_path):
    """AT68: no edge falls back to being a bare dependency."""
    runtime = planning_runtime(tmp_path)
    register_lab(runtime)
    await offer_work(runtime, lab_work(runtime))

    edges = plan_edges(runtime, only_plan(runtime))

    assert len(edges) == 5
    assert all(e.is_bound for e in edges)
    assert sorted(e.describe() for e in edges) == [
        "comparison -> comparison",
        "reading:measured -> reading:measured",
        "reading:reference -> reading:reference",
        "sample -> sample",
        "sample -> sample",
    ]
    runtime.close()


async def test_the_trace_explains_where_every_value_came_from(tmp_path):
    """AT69: provenance for the composed arrangement, port by port."""
    runtime = planning_runtime(tmp_path)
    register_lab(runtime)
    await offer_work(runtime, lab_work(runtime))

    trace = runtime.get_plan_trace(only_plan(runtime).id)
    into_compare = {b.input_name: b.producer_node_key for b in trace.bindings_into("compare:v1")}

    assert into_compare == {"measured": "measure:v1", "reference": "lookup_reference:v1"}
    assert trace.inputs_given["write_report:v1"] == {
        "comparison": "comparison-from-compare"
    }
    runtime.close()


async def test_the_same_arrangement_comes_out_of_a_sliced_run(tmp_path):
    """AT70: bindings and pacing are independent (Invariants 66 and 72)."""
    runtime = planning_runtime(tmp_path, "sliced.db")
    register_lab(runtime)
    work = lab_work(runtime)
    budget = DrainBudget(max_activations=1)

    await runtime.submit_event(work_required(work), budget)
    for _ in range(200):
        if runtime.last_drain.idle:
            break
        await runtime.drain(budget)

    plan = only_plan(runtime)
    assert plan.status is PlanStatus.COMPLETED
    assert received_by("compare:v1") == {
        "measured": "measured-from-measure",
        "reference": "reference-from-lookup_reference",
    }
    for name in LAB_CAPABILITIES:
        assert len(instances_named(runtime, name)) == 1
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_an_ambiguity_anywhere_stops_the_whole_arrangement(tmp_path):
    """AT71: one unclear input is enough; the rest of the plan does not run."""
    runtime = planning_runtime(tmp_path)
    register_lab(runtime)
    # A second, keyless producer of a reading: nothing now says which reading
    # ``compare`` should be given, so no plan may be committed.
    register_step(runtime, "stray_probe", "stray_probe", ("sample",), ("reading",))
    register_step(
        runtime, "summarize", "summarize", ("reading",), ("summary",)
    )
    work = lab_work(
        runtime,
        work_key="ambiguous_lab",
        required=LAB_CAPABILITIES + ("stray_probe", "summarize"),
        outputs=("analysis_report", "summary"),
    )

    await offer_work(runtime, work)

    assert runtime.get_plans() == []
    # Every capability exists; it is the ambiguous arrangement that cannot be
    # accepted.  Phase 4C distinguishes that from a competence gap.
    assert status_of(runtime, work) is WorkStatus.BLOCKED_PLAN
    assert RECORDED == []  # nothing ran on a plan that was never committed
    runtime.close()


async def test_nothing_in_this_phase_needed_an_llm(tmp_path):
    """AT72: the floor this phase deliberately sets (Invariant 64).

    Composition, binding, validation and pacing are all decided by exact
    symbolic rules.  Phase 4C may add judgement on top; it may not be needed
    underneath.
    """
    runtime = planning_runtime(tmp_path)
    register_lab(runtime)
    await offer_work(runtime, lab_work(runtime))

    for instance in runtime.process_store.all_instances():
        assert runtime.get_llm_invocations(instance.id) == []
    assert only_plan(runtime).planner_name == "composition"
    runtime.close()
