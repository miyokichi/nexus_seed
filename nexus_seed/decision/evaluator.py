"""PlanEvaluator — what a plan is estimated to cost, take, risk and yield.

Deterministic and metadata-driven (spec §11).  There is no predictive model
here and no learning from past runs (spec §102): each node contributes what its
ProcessDefinition *declares*, and the plan's figures are a fixed aggregation of
those.  That is a floor, not a placeholder — before a system can learn which
arrangement works out better, it needs a reproducible account of what it
expected, and an audit record that explains the expectation.

Declared like this::

    ProcessDefinition.metadata = {
        "decision_metadata": {
            "cost": 2.0, "latency": 5.0, "risk": 0.2,
            "quality": 0.9, "reliability": 0.95,
        }
    }

Each aggregation rule is a choice, so each one is stated:

    cost         sum       every node's cost is paid
    latency      critical path — a branching plan really is faster
    risk         max       the riskiest step is the plan's exposure
    reliability  product   every node has to work
    quality      min       a chain is as good as its weakest link
"""

from __future__ import annotations

from ..planning.fingerprint import plan_fingerprint
from .models import METRICS, UNKNOWN_RISK, PlanEvaluation

#: Where a definition declares its estimates.
METADATA_KEY = "decision_metadata"

#: A definition may flag that running it always needs a human first.
APPROVAL_KEY = "human_approval_required"


class PlanEvaluator:
    """Turns a plan's nodes and edges into a :class:`PlanEvaluation`."""

    def __init__(self, definitions: list | None = None) -> None:
        self._by_key = {(d.name, d.version): d for d in (definitions or ())}

    def with_definitions(self, definitions: list) -> "PlanEvaluator":
        """A copy of this evaluator looking at a different definition set."""
        return PlanEvaluator(definitions)

    # --- entry point -------------------------------------------------------

    def evaluate(
        self, plan, nodes: list, edges: list, *, planner_rank: int = 0
    ) -> PlanEvaluation:
        """Evaluate one plan.  Same inputs, same output, always (spec §103).

        ``planner_rank`` is where the deterministic planner put this candidate
        in its own order — which already accounts for capability priority and
        coverage.  Carried here so that when two plans score identically the
        planner's preference decides, rather than a hash.
        """
        declared = [self._declared(node) for node in nodes]
        reasons: list[str] = []

        evaluation = PlanEvaluation(
            plan_id=plan.id,
            work_requirement_id=getattr(plan, "work_requirement_id", None),
            fingerprint=plan_fingerprint(nodes, edges),
            node_count=len(nodes),
            depth=max((n.depth for n in nodes), default=-1) + 1,
            estimated_cost=_sum(d.get("cost") for d in declared),
            estimated_latency=self._critical_path_latency(nodes, edges, declared),
            estimated_risk=self._risk(declared, reasons),
            estimated_quality=_minimum(d.get("quality") for d in declared),
            estimated_reliability=_product(d.get("reliability") for d in declared),
            human_approval_required=any(d.get(APPROVAL_KEY) for d in declared),
            reasons=reasons,
            metadata={
                "nodes": [n.node_key for n in nodes],
                "planner_rank": planner_rank,
            },
        )

        undeclared = [
            n.node_key for n, d in zip(nodes, declared) if not d
        ]
        if undeclared:
            reasons.append(f"no decision metadata for {sorted(undeclared)}")
        unknown = evaluation.unknown_metrics
        if unknown:
            reasons.append(f"unknown: {unknown}")
        return evaluation

    def evaluate_all(self, plans_with_graph) -> list[PlanEvaluation]:
        """Evaluate a sequence of ``(plan, nodes, edges)`` triples."""
        return [self.evaluate(p, n, e) for p, n, e in plans_with_graph]

    # --- per-node declarations ---------------------------------------------

    def _declared(self, node) -> dict:
        """What this node's definition declares, as plain floats.

        A definition nobody annotated yields ``{}`` — every metric unknown.
        Deliberately not defaults: see :data:`UNKNOWN_RISK`.
        """
        definition = self._by_key.get((node.definition_name, node.definition_version))
        metadata = getattr(definition, "metadata", None) or {}
        raw = metadata.get(METADATA_KEY)
        declared: dict = {}
        if isinstance(raw, dict):
            for metric in METRICS:
                value = _as_float(raw.get(metric))
                if value is not None:
                    declared[metric] = value
            if raw.get(APPROVAL_KEY):
                declared[APPROVAL_KEY] = True
        if metadata.get(APPROVAL_KEY):
            declared[APPROVAL_KEY] = True
        return declared

    # --- aggregation rules --------------------------------------------------

    @staticmethod
    def _risk(declared: list[dict], reasons: list[str]) -> float:
        """The riskiest node is the plan's exposure (spec §13).

        A node that declared no risk contributes :data:`UNKNOWN_RISK` rather
        than nothing.  Treating silence as safety would let an unannotated
        process slip under any ``max_risk`` at all, which is the one failure
        mode a risk constraint exists to prevent (spec §12).
        """
        if not declared:
            return 0.0
        risks = []
        for entry in declared:
            value = entry.get("risk")
            if value is None:
                risks.append(UNKNOWN_RISK)
            else:
                risks.append(value)
        if any(entry.get("risk") is None for entry in declared):
            reasons.append(f"undeclared risk treated as {UNKNOWN_RISK}")
        return max(risks)

    @staticmethod
    def _critical_path_latency(nodes, edges, declared) -> float | None:
        """The longest path through the DAG, by node latency (spec §15).

        Summing every node would say a branching plan is as slow as a chain,
        which is the opposite of why one would branch.  Unknown anywhere makes
        the whole figure unknown: a path missing a term is not a shorter path.
        """
        latencies = {}
        for node, entry in zip(nodes, declared):
            value = entry.get("latency")
            if value is None:
                return None
            latencies[node.id] = value

        successors: dict = {n.id: [] for n in nodes}
        indegree: dict = {n.id: 0 for n in nodes}
        for edge in edges or ():
            if edge.from_node_id in successors and edge.to_node_id in indegree:
                successors[edge.from_node_id].append(edge.to_node_id)
                indegree[edge.to_node_id] += 1

        # Longest path by depth order.  The graph is a validated DAG, so a
        # depth-then-key ordering visits every producer before its consumers.
        longest = {n.id: latencies[n.id] for n in nodes}
        for node in sorted(nodes, key=lambda n: (n.depth, n.node_key)):
            for successor in successors[node.id]:
                longest[successor] = max(
                    longest[successor], longest[node.id] + latencies[successor]
                )
        return max(longest.values(), default=None)


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum(values) -> float | None:
    """Total, or unknown if any term is unknown (spec §12)."""
    total = 0.0
    seen = False
    for value in values:
        if value is None:
            return None
        total += value
        seen = True
    return total if seen else None


def _product(values) -> float | None:
    total = 1.0
    seen = False
    for value in values:
        if value is None:
            return None
        total *= value
        seen = True
    return total if seen else None


def _minimum(values) -> float | None:
    known = []
    for value in values:
        if value is None:
            return None
        known.append(value)
    return min(known) if known else None


__all__ = ["APPROVAL_KEY", "METADATA_KEY", "PlanEvaluator"]
