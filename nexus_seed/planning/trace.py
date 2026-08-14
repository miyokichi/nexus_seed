"""Plan trace — who did what, in what order, and why this shape.

The question a composed plan raises that a single process never did: *the
system assembled a temporary arrangement of several processes — on what
grounds, and how far did it get?*
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.process import ProcessInstance
    from ..work.work_requirement import WorkRequirement
    from .models import PlanEdge, PlanNode, ProcessPlan


@dataclass
class PlanNodeTrace:
    """One position in a plan, with whatever ran there."""

    node: "PlanNode"
    instance: "ProcessInstance | None" = None

    @property
    def status(self) -> str:
        return self.node.status.value

    @property
    def output(self) -> dict | None:
        """What the process at this position produced, if it has run."""
        if self.instance is None:
            return None
        return self.instance.local_state.get("output")


@dataclass
class PlanBinding:
    """One resolved data connection, in the terms a reader would ask about.

    Phase 4B could only say *this node ran before that one*.  Since bindings
    are explicit (Invariant 66) the trace can answer the question that actually
    matters after the fact: **where did this value come from?** — down to the
    port, not just the process (spec §24).
    """

    producer_node_key: str
    producer_port: str
    consumer_node_key: str
    consumer_port: str
    input_name: str
    bound: bool = True

    def describe(self) -> str:
        """``producer.port -> consumer.port``, for logs and reasons."""
        arrow = "->" if self.bound else "~>"
        return (
            f"{self.producer_node_key}.{self.producer_port} {arrow} "
            f"{self.consumer_node_key}.{self.consumer_port}"
        )


@dataclass
class PlanTrace:
    """The lineage and progress of one composed plan."""

    plan: "ProcessPlan"
    nodes: list[PlanNodeTrace] = field(default_factory=list)
    edges: list["PlanEdge"] = field(default_factory=list)
    requirement: "WorkRequirement | None" = None
    capability_matches: list = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.plan.status.value

    @property
    def node_statuses(self) -> dict[str, str]:
        """``{node_key: status}`` — how far the plan got."""
        return {t.node.node_key: t.status for t in self.nodes}

    @property
    def order(self) -> list[str]:
        """Node keys in execution order (shallowest stage first)."""
        return [t.node.node_key for t in self.nodes]

    @property
    def outputs(self) -> dict[str, dict | None]:
        """``{node_key: output}`` for every position that has run."""
        return {t.node.node_key: t.output for t in self.nodes if t.output is not None}

    @property
    def instances(self) -> list["ProcessInstance"]:
        """Every process instance the plan created."""
        return [t.instance for t in self.nodes if t.instance is not None]

    @property
    def reasons(self) -> list[str]:
        """Why this plan shape was chosen."""
        return list(self.plan.reasons)

    @property
    def edge_pairs(self) -> list[tuple[str, str, str | None]]:
        """``(from_key, to_key, artifact_type)`` for each dependency."""
        keys = {t.node.id: t.node.node_key for t in self.nodes}
        return [
            (keys.get(e.from_node_id, "?"), keys.get(e.to_node_id, "?"), e.artifact_type)
            for e in self.edges
        ]

    @property
    def bindings(self) -> list[PlanBinding]:
        """Every data connection, producer port to consumer port (spec §24).

        ``bound`` is False for a Phase 4B edge, which recorded only a type: the
        trace shows what it must have meant without pretending it was stated.
        """
        keys = {t.node.id: t.node.node_key for t in self.nodes}
        return [
            PlanBinding(
                producer_node_key=keys.get(e.from_node_id, "?"),
                producer_port=str(e.output_port),
                consumer_node_key=keys.get(e.to_node_id, "?"),
                consumer_port=str(e.input_port),
                input_name=e.input_port.name,
                bound=e.is_bound,
            )
            for e in self.edges
        ]

    @property
    def binding_pairs(self) -> list[tuple[str, str, str, str]]:
        """``(producer_key, producer_port, consumer_key, consumer_port)``."""
        return [
            (b.producer_node_key, b.producer_port, b.consumer_node_key, b.consumer_port)
            for b in self.bindings
        ]

    def bindings_into(self, node_key: str) -> list[PlanBinding]:
        """The bindings that feed one position — what it was given, and by whom."""
        return [b for b in self.bindings if b.consumer_node_key == node_key]

    @property
    def inputs_given(self) -> dict[str, dict]:
        """``{node_key: {input_name: value}}`` — what each position received.

        Read back from the spawned instances rather than recomputed, so this
        says what actually happened, not what should have.
        """
        given: dict[str, dict] = {}
        for trace in self.nodes:
            if trace.instance is None:
                continue
            received = trace.instance.input.get("inputs")
            if isinstance(received, dict):
                given[trace.node.node_key] = dict(received)
        return given


def get_plan_trace(
    plan_id,
    *,
    plan_store,
    process_store,
    work_requirement_store=None,
    capability_store=None,
) -> PlanTrace | None:
    """Resolve a plan's structure, progress and provenance from storage."""
    plan = plan_store.get(plan_id)
    if plan is None:
        return None

    nodes = [
        PlanNodeTrace(
            node=node,
            instance=(
                process_store.get_instance(node.process_instance_id)
                if node.process_instance_id
                else None
            ),
        )
        for node in plan_store.nodes(plan.id)
    ]
    requirement = (
        work_requirement_store.get(plan.work_requirement_id)
        if work_requirement_store is not None
        else None
    )
    matches = (
        capability_store.matches_for(plan.work_requirement_id)
        if capability_store is not None
        else []
    )
    return PlanTrace(
        plan=plan,
        nodes=nodes,
        edges=plan_store.edges(plan.id),
        requirement=requirement,
        capability_matches=matches,
    )
