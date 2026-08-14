"""A plan's shape, rather than its generated ids, is its decision identity."""

from __future__ import annotations

import uuid

from nexus_seed.planning.fingerprint import dedupe_by_fingerprint, plan_fingerprint
from nexus_seed.planning.models import PlanCandidate, PlanEdge, PlanNode


def graph(*, producer_id=None, consumer_id=None, output_key=None):
    plan_id = uuid.uuid4()
    producer = PlanNode(
        plan_id=plan_id,
        node_key="produce:v1",
        definition_name="produce",
        definition_version="1",
        provided_capabilities=["produce"],
        output_types=["artifact" if output_key is None else f"artifact:{output_key}"],
        id=producer_id or uuid.uuid4(),
    )
    consumer = PlanNode(
        plan_id=plan_id,
        node_key="consume:v1",
        definition_name="consume",
        definition_version="1",
        provided_capabilities=["consume"],
        input_types=["artifact" if output_key is None else f"artifact:{output_key}"],
        id=consumer_id or uuid.uuid4(),
    )
    edge = PlanEdge(
        plan_id=plan_id,
        from_node_id=producer.id,
        to_node_id=consumer.id,
        output_type="artifact",
        output_key=output_key,
        input_type="artifact",
        input_key=output_key,
    )
    return [producer, consumer], [edge]


def test_fingerprint_ignores_plan_and_node_ids() -> None:
    nodes_a, edges_a = graph()
    nodes_b, edges_b = graph()

    assert plan_fingerprint(nodes_a, edges_a) == plan_fingerprint(nodes_b, edges_b)


def test_fingerprint_includes_port_binding() -> None:
    nodes_a, edges_a = graph(output_key="left")
    nodes_b, edges_b = graph(output_key="right")

    assert plan_fingerprint(nodes_a, edges_a) != plan_fingerprint(nodes_b, edges_b)


def test_structural_duplicates_are_offered_once() -> None:
    nodes_a, edges_a = graph()
    nodes_b, edges_b = graph()
    candidates = [
        PlanCandidate(nodes=nodes_a, edges=edges_a),
        PlanCandidate(nodes=nodes_b, edges=edges_b),
    ]

    assert dedupe_by_fingerprint(candidates) == [candidates[0]]
