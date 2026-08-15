"""PlanValidator — nothing runs a plan the planner merely proposed.

The same boundary Phases 3B and 3C drew for interpretations and actions
(spec §69): the component that *generates* a candidate is not the component
that decides it may proceed.  Here it earns its keep twice over — once at
composition time, and again just before execution, because the world can move
between the two (a definition disabled, a capability withdrawn).
"""

from __future__ import annotations

from .models import (
    BindingStatus,
    PlanCandidate,
    PlanValidation,
    Port,
    SearchBounds,
    parse_ports,
)


class PlanValidator:
    """Checks that a plan is a runnable DAG that actually does the job."""

    def __init__(
        self, registry, *, bounds: SearchBounds | None = None, provider_registry=None
    ) -> None:
        self.registry = registry
        self.bounds = bounds or SearchBounds()
        self.provider_registry = provider_registry

    def validate_candidate(
        self,
        candidate: PlanCandidate,
        definitions: list,
        *,
        available_input_types: list[str] | None = None,
        required_output_types: list[str] | None = None,
    ) -> PlanValidation:
        """Validate a freshly composed candidate."""
        result = PlanValidation()
        by_key = {(d.name, d.version): d for d in definitions}

        if not candidate.nodes:
            result.fail("plan has no nodes")
            return result
        if len(candidate.nodes) > self.bounds.max_plan_nodes:
            result.fail(
                f"plan has {len(candidate.nodes)} nodes, over the limit of "
                f"{self.bounds.max_plan_nodes}"
            )

        self._check_definitions(candidate, by_key, result)
        self._check_dag(candidate, result)
        self._check_data_flow(candidate, available_input_types or [], result)

        if candidate.missing_capabilities:
            result.fail(f"capabilities not covered: {sorted(candidate.missing_capabilities)}")
        missing_outputs = [
            t
            for t in (required_output_types or [])
            if t not in candidate.produced_output_types
        ]
        if missing_outputs:
            result.fail(f"required outputs not produced: {sorted(missing_outputs)}")

        candidate.valid = result.ok
        candidate.reasons.extend(result.reasons)
        return result

    def validate_before_execution(
        self, nodes: list, definitions: list, *, edges: list | None = None
    ) -> PlanValidation:
        """Re-check a stored plan just before running its next nodes.

        A plan is written against the system as it was; between validation and
        execution a definition may have been disabled or removed.  Discovering
        that when the node is about to spawn is much better than spawning it
        (spec §67).
        """
        result = PlanValidation()
        by_key = {(d.name, d.version): d for d in definitions}
        for node in nodes:
            definition = by_key.get(node.key)
            if definition is None:
                result.fail(f"{node.node_key}: definition no longer registered")
                continue
            if not _enabled(definition):
                result.fail(f"{node.node_key}: definition is disabled")
                continue
            if not self._capabilities_live(node):
                result.fail(f"{node.node_key}: capabilities no longer provided")
            if (
                self.provider_registry is not None
                and not self.provider_registry.has_eligible_provider(definition)
            ):
                result.fail(f"{node.node_key}: no eligible execution provider")
        self._check_legacy_bindings(nodes, edges or [], result)
        return result

    @staticmethod
    def _check_legacy_bindings(nodes, edges, result: PlanValidation) -> None:
        """Refuse a pre-4B.1 edge whose meaning is not beyond doubt (spec §58)."""
        by_id = {n.id: n for n in nodes}
        for edge in edges:
            consumer = by_id.get(edge.to_node_id)
            if consumer is None or edge.is_bound:
                continue
            if resolve_legacy_binding(edge, None, consumer) is None:
                result.fail(
                    f"{consumer.node_key}: legacy edge for {edge.artifact_type!r} is "
                    "ambiguous; re-plan rather than guess",
                    BindingStatus.AMBIGUOUS_BINDING,
                )

    # --- individual checks -------------------------------------------------

    def _check_definitions(self, candidate, by_key, result) -> None:
        for node in candidate.nodes:
            definition = by_key.get(node.key)
            if definition is None:
                result.fail(f"{node.node_key}: no such ProcessDefinition")
            elif not _enabled(definition):
                result.fail(f"{node.node_key}: ProcessDefinition is disabled")
            elif (
                self.provider_registry is not None
                and not self.provider_registry.has_eligible_provider(definition)
            ):
                result.fail(f"{node.node_key}: no eligible execution provider")

    @staticmethod
    def _check_dag(candidate: PlanCandidate, result: PlanValidation) -> None:
        """Reject cycles (Invariant 58).

        A loop belongs *inside* a process, expressed with a continuation — a
        cyclic plan graph would have no meaningful completion condition.
        """
        node_ids = {n.id for n in candidate.nodes}
        adjacency: dict = {n.id: [] for n in candidate.nodes}
        for edge in candidate.edges:
            if edge.from_node_id not in node_ids or edge.to_node_id not in node_ids:
                result.fail("edge references a node outside the plan")
                continue
            adjacency[edge.from_node_id].append(edge.to_node_id)

        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(adjacency, WHITE)

        def visit(node_id) -> bool:
            colour[node_id] = GREY
            for successor in adjacency[node_id]:
                if colour[successor] == GREY:
                    return True
                if colour[successor] == WHITE and visit(successor):
                    return True
            colour[node_id] = BLACK
            return False

        for node_id in list(adjacency):
            if colour[node_id] == WHITE and visit(node_id):
                result.fail("plan contains a cycle")
                return

    @staticmethod
    def _check_data_flow(
        candidate: PlanCandidate, available: list[str], result: PlanValidation
    ) -> None:
        """Every required input must have exactly one source (Invariant 67).

        Capability coverage alone is not enough: processes that together *can*
        do the work are useless if none of them can be fed (spec §19).  And a
        source has to be *the* source — one incoming edge or an initial input,
        never "whichever happened to run first".
        """
        by_id = {n.id: n for n in candidate.nodes}
        initial = parse_ports(available)

        # Group the edges by the consumer input they claim to feed.
        incoming: dict = {}
        for edge in candidate.edges:
            if edge.to_node_id not in by_id or edge.from_node_id not in by_id:
                result.fail("edge references a node outside the plan", BindingStatus.INVALID_PORT)
                continue
            producer = by_id[edge.from_node_id]
            consumer = by_id[edge.to_node_id]

            if edge.is_bound and edge.output_port.type != edge.input_port.type:
                result.fail(
                    f"{consumer.node_key}.{edge.input_port}: type mismatch with "
                    f"{producer.node_key}.{edge.output_port}",
                    BindingStatus.TYPE_MISMATCH,
                )
                continue
            if edge.is_bound and not _declares(producer.output_types, edge.output_port):
                result.fail(
                    f"{producer.node_key} does not produce {edge.output_port}",
                    BindingStatus.INVALID_PORT,
                )
                continue
            if edge.is_bound and not _declares_exactly(
                consumer.input_types, edge.input_port
            ):
                result.fail(
                    f"{consumer.node_key} does not consume {edge.input_port}",
                    BindingStatus.INVALID_PORT,
                )
                continue
            incoming.setdefault((edge.to_node_id, str(edge.input_port)), []).append(edge)

        # Inputs the planner already knew it could not bind.  Reported as what
        # they are rather than as "nothing supplies this" — the difference
        # between *no producer* and *too many* is exactly what a reader needs.
        undecided = {
            (a.get("node"), a.get("port")) for a in candidate.ambiguous_bindings
        }
        for problem in candidate.ambiguous_bindings:
            result.fail(
                f"{problem.get('node')}.{problem.get('port')}: "
                f"{problem.get('sources')} could all supply this input, and "
                "nothing says which",
                BindingStatus.AMBIGUOUS_BINDING,
            )

        for node in sorted(candidate.nodes, key=lambda n: (n.depth, n.node_key)):
            for port in parse_ports(node.input_types):
                if (node.node_key, str(port)) in undecided:
                    continue
                sources = incoming.get((node.id, str(port)), [])
                if len(sources) > 1:
                    # Fan-in aggregation is not a thing Phase 4B.1 defines
                    # (spec §20); two producers for one input is a plan bug.
                    result.fail(
                        f"{node.node_key}.{port}: bound to "
                        f"{len(sources)} producers",
                        BindingStatus.DUPLICATE_BINDING,
                    )
                    continue
                if sources:
                    continue
                if any(port.matches(p) for p in initial):
                    continue
                result.fail(
                    f"{node.node_key}.{port}: nothing supplies this input",
                    BindingStatus.MISSING_INPUT,
                )

    def _capabilities_live(self, node) -> bool:
        provided = {
            c.name
            for c in self.registry.get_capabilities_for_process(
                node.definition_name, node.definition_version
            )
            if c.enabled
        }
        return all(name in provided for name in node.provided_capabilities)


def _enabled(definition) -> bool:
    return bool((definition.metadata or {}).get("enabled", True))


def _declares(specs: list[str], port: Port) -> bool:
    """Whether a node declares a port compatible with ``port``.

    Used on the *producing* side, where a keyless declaration is a wildcard: a
    process that says it emits ``m`` may be drawn from as ``m`` or ``m:anything``
    (spec §10).
    """
    return any(port.matches(declared) for declared in parse_ports(specs))


def _declares_exactly(specs: list[str], port: Port) -> bool:
    """Whether a node declares precisely this port.

    Used on the *consuming* side, where the wildcard rule must not apply: the
    input port's name is the key the value arrives under, so an edge naming a
    port the consumer never declared would hand the handler a value under a
    name it does not read — silently, and only at run time.
    """
    return any(port == declared for declared in parse_ports(specs))


def resolve_legacy_binding(edge, producer, consumer) -> Port | None:
    """Work out what a Phase 4B edge must have meant, if it is unambiguous.

    Old edges name only an ``artifact_type``.  They are honoured when exactly
    one interpretation exists, and refused otherwise — guessing would reproduce
    the very ambiguity this phase removed (spec §58).
    """
    if edge.is_bound:
        return edge.input_port
    wanted = Port(edge.artifact_type or "")
    candidates = [p for p in parse_ports(consumer.input_types) if wanted.matches(p)]
    return candidates[0] if len(candidates) == 1 else None
