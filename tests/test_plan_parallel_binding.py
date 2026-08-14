"""AT25–AT28 (spec §14, §17, §37): what fan-out and fan-in do to bindings.

Branching is not only a scheduling question.  Once several nodes run in one
stage, several edges are resolved in one activation — and the old type-keyed
lookup would have had them overwrite each other silently.  These are the shapes
that make the difference visible.
"""

from __future__ import annotations

from planning_helpers import (
    RECORDED,
    make_work,
    offer_work,
    only_plan,
    plan_edges,
    planning_runtime,
    received_by,
    register_step,
    status_of,
)

from nexus_seed.work.work_requirement import WorkStatus


async def test_one_producing_port_feeds_many_consumers(tmp_path):
    """AT25: fan-out is not a duplicate binding — the *inputs* are distinct."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "source", "source", ("raw",), ("m",))
    register_step(runtime, "left", "left", ("m",), ("l",))
    register_step(runtime, "right", "right", ("m",), ("r",))
    register_step(runtime, "sink", "sink", ("l", "r"), ("out",))
    work = make_work(
        runtime,
        required=("source", "left", "right", "sink"),
        inputs=("raw",),
        outputs=("out",),
    )

    await offer_work(runtime, work)

    edges = plan_edges(runtime, only_plan(runtime))
    from_source = [e for e in edges if e.output_port.type == "m"]
    assert len(from_source) == 2
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_one_node_feeds_two_keyed_inputs_of_another(tmp_path):
    """AT26: two edges between the same pair of nodes, and both survive.

    Phase 4B's uniqueness rule was ``(plan, producer, consumer, type)``, which
    silently dropped the second of these — the consumer then ran with one input
    missing while the plan still reported success.
    """
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "split", "split", ("raw",), ("m:left", "m:right"))
    register_step(runtime, "join", "join", ("m:left", "m:right"), ("out",))
    work = make_work(
        runtime, required=("split", "join"), inputs=("raw",), outputs=("out",)
    )

    await offer_work(runtime, work)

    edges = plan_edges(runtime, only_plan(runtime))
    assert sorted(e.describe() for e in edges) == [
        "m:left -> m:left",
        "m:right -> m:right",
    ]
    assert received_by("join:v1") == {
        "left": "left-from-split",
        "right": "right-from-split",
    }
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()


async def test_two_parallel_producers_of_one_type_do_not_overwrite_each_other(tmp_path):
    """AT27: the failure mode the type-keyed lookup had.

    Two nodes in the *same stage* both produce ``m``, keyed differently, and
    both feed the same consumer.  Under Phase 4B whichever was visited last
    won; the value the consumer got depended on iteration order.
    """
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "probe_a", "probe_a", ("raw",), ("m:a",))
    register_step(runtime, "probe_b", "probe_b", ("raw",), ("m:b",))
    register_step(runtime, "compare", "compare", ("m:a", "m:b"), ("out",))
    work = make_work(
        runtime,
        required=("probe_a", "probe_b", "compare"),
        inputs=("raw",),
        outputs=("out",),
    )

    await offer_work(runtime, work)

    assert received_by("compare:v1") == {
        "a": "a-from-probe_a",
        "b": "b-from-probe_b",
    }
    runtime.close()


async def test_a_stage_resolves_every_binding_from_the_same_activation(tmp_path):
    """AT28: the nodes of one stage were spawned together, inputs and all."""
    runtime = planning_runtime(tmp_path)
    register_step(runtime, "source", "source", ("raw",), ("m",))
    register_step(runtime, "left", "left", ("m",), ("l",))
    register_step(runtime, "right", "right", ("m",), ("r",))
    work = make_work(
        runtime,
        required=("source", "left", "right"),
        inputs=("raw",),
        outputs=("l", "r"),
    )

    await offer_work(runtime, work)

    order = [r["node"] for r in RECORDED]
    assert order[0] == "source:v1"
    assert set(order[1:]) == {"left:v1", "right:v1"}
    # Both parallel nodes were handed the same producing port's value.
    assert received_by("left:v1") == {"m": "m-from-source"}
    assert received_by("right:v1") == {"m": "m-from-source"}
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    runtime.close()
