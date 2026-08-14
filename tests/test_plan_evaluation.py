"""AT1, AT2 (spec §103, §104): what a plan is estimated to cost, take and risk.

Deterministic and metadata-driven.  There is no prediction here and nothing is
learned from past runs (spec §102) — each aggregation rule is a stated choice,
and the point of these tests is that each one is the rule it claims to be.

The rule that carries the most weight is the one about *silence*: a metric
nobody declared is unknown, not zero (spec §12).  Zero would make the cheapest
plan the one nobody bothered to describe, and the safest plan the one nobody
assessed.
"""

from __future__ import annotations

import uuid

from decision_helpers import (
    ANALYZE,
    CAREFUL,
    FAST,
    choice_work,
    compose_only,
    offer_work,
    planning_runtime,
    register_alternatives,
    register_step,
)

from nexus_seed.decision.evaluator import PlanEvaluator
from nexus_seed.decision.models import UNKNOWN_RISK
from nexus_seed.planning.models import PlanNode, ProcessPlan


def graph(*specs, edges=()):
    """A bare plan graph: ``(definition_name, depth)`` pairs."""
    plan = ProcessPlan(work_requirement_id=uuid.uuid4())
    nodes = [
        PlanNode(
            plan_id=plan.id,
            node_key=f"{name}:v1",
            definition_name=name,
            definition_version="1",
            depth=depth,
        )
        for name, depth in specs
    ]
    by_name = {n.definition_name: n for n in nodes}
    from nexus_seed.planning.models import PlanEdge

    built = [
        PlanEdge(
            plan_id=plan.id,
            from_node_id=by_name[a].id,
            to_node_id=by_name[b].id,
            output_type="m",
            input_type="m",
        )
        for a, b in edges
    ]
    return plan, nodes, built


class FakeDefinition:
    def __init__(self, name, metadata=None, version="1"):
        self.name = name
        self.version = version
        self.metadata = metadata or {}


def declared(name, **values):
    return FakeDefinition(name, {"decision_metadata": values})


# --- aggregation rules -----------------------------------------------------


def test_cost_is_the_sum_of_the_nodes():
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluator = PlanEvaluator([declared("a", cost=2.0), declared("b", cost=3.0)])

    assert evaluator.evaluate(plan, nodes, edges).estimated_cost == 5.0


def test_risk_is_the_riskiest_node():
    """Spec §13: the plan's exposure is its worst step, not its average."""
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluator = PlanEvaluator([declared("a", risk=0.1), declared("b", risk=0.7)])

    assert evaluator.evaluate(plan, nodes, edges).estimated_risk == 0.7


def test_reliability_is_the_product():
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluator = PlanEvaluator(
        [declared("a", reliability=0.9), declared("b", reliability=0.5)]
    )

    assert evaluator.evaluate(plan, nodes, edges).estimated_reliability == 0.45


def test_quality_is_the_weakest_link():
    """Spec §17: a fixed rule, stated rather than averaged into meaninglessness."""
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluator = PlanEvaluator([declared("a", quality=0.9), declared("b", quality=0.6)])

    assert evaluator.evaluate(plan, nodes, edges).estimated_quality == 0.6


def test_latency_follows_the_critical_path_not_the_total():
    """AT2 (spec §15): the whole point of branching is that it is faster.

    ``P1 -> {P2, P3} -> P4`` with latencies 1, 10, 2, 1.  Summing gives 14;
    the longest path is 1 + 10 + 1 = 12, and P3 costs nothing extra because it
    runs alongside P2.
    """
    plan, nodes, edges = graph(
        ("p1", 0), ("p2", 1), ("p3", 1), ("p4", 2),
        edges=(("p1", "p2"), ("p1", "p3"), ("p2", "p4"), ("p3", "p4")),
    )
    evaluator = PlanEvaluator(
        [
            declared("p1", latency=1.0),
            declared("p2", latency=10.0),
            declared("p3", latency=2.0),
            declared("p4", latency=1.0),
        ]
    )

    assert evaluator.evaluate(plan, nodes, edges).estimated_latency == 12.0


def test_a_chain_has_no_critical_path_advantage():
    plan, nodes, edges = graph(
        ("a", 0), ("b", 1), ("c", 2), edges=(("a", "b"), ("b", "c"))
    )
    evaluator = PlanEvaluator(
        [declared("a", latency=1.0), declared("b", latency=2.0), declared("c", latency=4.0)]
    )

    assert evaluator.evaluate(plan, nodes, edges).estimated_latency == 7.0


# --- unknown is not zero ---------------------------------------------------


def test_an_undeclared_metric_is_unknown_not_zero():
    """Spec §12: the load-bearing rule of this whole layer."""
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluator = PlanEvaluator([declared("a", cost=2.0), FakeDefinition("b")])

    evaluation = evaluator.evaluate(plan, nodes, edges)

    assert evaluation.estimated_cost is None
    assert "cost" in evaluation.unknown_metrics


def test_an_undeclared_risk_is_conservative_not_safe():
    """Reading silence as safety is how a risk policy stops meaning anything."""
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluator = PlanEvaluator([declared("a", risk=0.1), FakeDefinition("b")])

    evaluation = evaluator.evaluate(plan, nodes, edges)

    assert evaluation.estimated_risk == UNKNOWN_RISK
    assert UNKNOWN_RISK > 0.1
    assert any("undeclared risk" in r for r in evaluation.reasons)


def test_a_plan_of_wholly_undeclared_nodes_says_so():
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluation = PlanEvaluator([FakeDefinition("a"), FakeDefinition("b")]).evaluate(
        plan, nodes, edges
    )

    assert evaluation.unknown_metrics == ["cost", "latency", "quality", "reliability"]
    assert evaluation.estimated_risk == UNKNOWN_RISK
    assert any("no decision metadata" in r for r in evaluation.reasons)


def test_human_approval_is_carried_up_from_any_node():
    plan, nodes, edges = graph(("a", 0), ("b", 1))
    evaluator = PlanEvaluator(
        [FakeDefinition("a"), FakeDefinition("b", {"human_approval_required": True})]
    )

    assert evaluator.evaluate(plan, nodes, edges).human_approval_required


# --- determinism -----------------------------------------------------------


def test_the_same_plan_evaluates_the_same_way_every_time():
    """AT1."""
    plan, nodes, edges = graph(("a", 0), ("b", 1), edges=(("a", "b"),))
    evaluator = PlanEvaluator(
        [declared("a", cost=1.0, risk=0.2), declared("b", cost=2.0, risk=0.4)]
    )

    first = evaluator.evaluate(plan, nodes, edges)
    second = evaluator.evaluate(plan, nodes, edges)

    assert first.summary() == second.summary()


# --- through the real pipeline ---------------------------------------------


async def test_every_candidate_is_evaluated_when_it_is_composed(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    work = choice_work(runtime)
    compose_only(runtime)

    await offer_work(runtime, work)

    evaluations = runtime.get_plan_evaluations(work.id)
    assert len(evaluations) == 2
    by_risk = sorted(e.estimated_risk for e in evaluations)
    # fast: max(0.8, 0.1); careful: max(0.1, 0.1)
    assert by_risk == [ANALYZE["risk"], FAST["risk"]]
    runtime.close()


async def test_evaluations_are_read_back_after_a_restart(tmp_path):
    from nexus_seed.runtime.runtime import Runtime
    from planning_helpers import wire

    runtime = planning_runtime(tmp_path, "eval.db")
    register_alternatives(runtime)
    work = choice_work(runtime)
    compose_only(runtime)
    await offer_work(runtime, work)
    before = {e.plan_id: e.summary() for e in runtime.get_plan_evaluations(work.id)}
    runtime.close()

    runtime2 = wire(Runtime(tmp_path / "eval.db"))
    after = {e.plan_id: e.summary() for e in runtime2.get_plan_evaluations(work.id)}

    assert after == before
    runtime2.close()


async def test_a_costed_plan_reports_the_sum_of_its_declarations(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_step(
        runtime, "only_extract", "extract", ("raw",), ("m",), decision_metadata=CAREFUL
    )
    register_step(runtime, "analyze", "analyze", ("m",), ("report",), decision_metadata=ANALYZE)
    work = choice_work(runtime)

    await offer_work(runtime, work)

    evaluation = runtime.get_plan_evaluations(work.id)[0]
    assert evaluation.estimated_cost == CAREFUL["cost"] + ANALYZE["cost"]
    assert evaluation.estimated_latency == CAREFUL["latency"] + ANALYZE["latency"]
    assert evaluation.estimated_risk == max(CAREFUL["risk"], ANALYZE["risk"])
    runtime.close()
