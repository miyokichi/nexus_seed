"""CompositionPlanner — work out an order in which processes add up to a job.

Entirely deterministic (Invariant 64).  No LLM reads process descriptions and
guesses what fits; connection is decided by exact symbolic type equality
(Invariant 59).  That is a deliberate floor, not a placeholder: before anything
can *reason* about which composition is best, the system needs a reproducible
answer to whether one exists, and an audit record explaining the one it chose.

The search is backward-chaining and bounded (spec §21–§23).  Start from what
the work needs, ask who provides it, ask what *they* need, and stop — always —
at explicit limits.  A planner that can loop is a planner that can hang the
runtime.

The planner has no side effects (spec §132): it returns candidates.  Validating
and committing them is somebody else's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..capabilities.matcher import definition_enabled, definition_priority
from .fingerprint import candidate_fingerprint, dedupe_by_fingerprint
from .models import PlanCandidate, PlanEdge, PlanNode, Port, SearchBounds, parse_ports

logger = logging.getLogger("nexus_seed.planning")


@dataclass
class Provider:
    """A definition considered as a way to obtain something."""

    definition: object
    capabilities: list[str] = field(default_factory=list)
    input_ports: list[Port] = field(default_factory=list)
    output_ports: list[Port] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.definition.name, self.definition.version)

    @property
    def input_types(self) -> list[str]:
        """The bare types this provider consumes (ports without their keys)."""
        return _unique(p.type for p in self.input_ports)

    @property
    def output_types(self) -> list[str]:
        """The bare types this provider produces."""
        return _unique(p.type for p in self.output_ports)


class CompositionPlanner:
    """Builds candidate multi-process plans, deterministically and finitely."""

    name = "composition"
    version = "1"

    def __init__(self, registry, *, bounds: SearchBounds | None = None) -> None:
        self.registry = registry
        self.bounds = bounds or SearchBounds()
        self._definition_cache: dict = {}

    # --- entry point -------------------------------------------------------

    def plan(
        self,
        requirements: list,
        definitions: list,
        *,
        available_input_types: list[str] | None = None,
        required_output_types: list[str] | None = None,
    ) -> list[PlanCandidate]:
        """Return candidate plans, best first.

        An empty list means no composition was found within the bounds — which
        is a definite answer, not a timeout (spec §23).
        """
        # Candidate ranking uses capability priority.  Populate this before
        # sorting candidates; doing it later in ``score_candidates`` made every
        # priority contribution zero during the actual choice and left the
        # fingerprint to decide which provider came first.
        self._definition_cache = {(d.name, d.version): d for d in definitions}
        providers = self._providers(definitions)
        mandatory = [r for r in requirements if r.required]
        available = list(available_input_types or [])
        wanted_outputs = list(required_output_types or [])

        candidate = self._compose(providers, mandatory, available, wanted_outputs)
        candidates = [candidate] if candidate is not None else []

        # Alternatives give the ranker something to choose between and the
        # audit something to show.  Phase 4C selects among candidates rather
        # than taking the first valid one, so this enumeration is now what
        # makes a decision possible at all (spec §6).
        for alternative in self._alternatives(
            providers, mandatory, available, wanted_outputs, candidate
        ):
            candidates.append(alternative)
            if len(candidates) >= self.bounds.max_candidate_plans:
                break

        candidates.sort(key=self._rank, reverse=True)
        # Two plans with the same structure are the same plan; offering both as
        # a choice would be a choice about nothing (spec §7).
        return dedupe_by_fingerprint(candidates)

    # --- provider view -----------------------------------------------------

    def _providers(self, definitions: list) -> list[Provider]:
        """Describe each usable definition by what it needs and offers."""
        providers: list[Provider] = []
        for definition in definitions:
            if not definition_enabled(definition):
                continue
            capabilities = self.registry.get_capabilities_for_process(
                definition.name, definition.version
            )
            enabled = [c for c in capabilities if c.enabled]
            if not enabled:
                continue
            providers.append(
                Provider(
                    definition=definition,
                    capabilities=[c.name for c in enabled],
                    input_ports=_unique_ports(
                        p for c in enabled for p in parse_ports(c.input_types)
                    ),
                    output_ports=_unique_ports(
                        p for c in enabled for p in parse_ports(c.output_types)
                    ),
                )
            )
        # Stable order: priority, then name/version.  Everything downstream
        # inherits this determinism.
        providers.sort(
            key=lambda p: (-definition_priority(p.definition), p.key[0], p.key[1])
        )
        return providers

    # --- the search --------------------------------------------------------

    def _compose(
        self,
        providers: list[Provider],
        requirements: list,
        available: list[str],
        required_outputs: list[str],
        *,
        exclude: set[tuple[str, str]] | None = None,
        prefer: dict | None = None,
    ) -> PlanCandidate | None:
        """Build one plan by satisfying goals backwards, then ordering forwards.

        Goals are the required capabilities plus the required output types.
        For each, a provider is chosen; anything that provider needs becomes a
        new goal.  The result is then laid out in dependency order, which is
        also what proves the data actually flows.
        """
        exclude = exclude or set()
        chosen: dict[tuple[str, str], Provider] = {}
        goals: list[tuple[str, str]] = [("capability", r.name) for r in requirements]
        goals += [("type", t) for t in required_outputs]

        depth = 0
        while goals:
            depth += 1
            if depth > self.bounds.max_search_depth:
                logger.info("composition search hit max depth %d", self.bounds.max_search_depth)
                return None

            next_goals: list[tuple[str, str]] = []
            for kind, target in goals:
                if self._already_satisfied(kind, target, chosen, available):
                    continue
                provider = self._pick(kind, target, providers, exclude, prefer)
                if provider is None:
                    return None  # nothing provides this: not a composition problem
                if provider.key not in chosen:
                    chosen[provider.key] = provider
                    if len(chosen) > self.bounds.max_plan_nodes:
                        logger.info(
                            "composition exceeded max_plan_nodes %d",
                            self.bounds.max_plan_nodes,
                        )
                        return None
                    # Whatever this provider needs is now a goal in its own right.
                    next_goals += [
                        ("type", t)
                        for t in provider.input_types
                        if t not in available
                    ]
            goals = _unique_pairs(next_goals)

        return self._layout(chosen.values(), requirements, available, required_outputs)

    @staticmethod
    def _already_satisfied(kind, target, chosen, available) -> bool:
        if kind == "capability":
            return any(target in p.capabilities for p in chosen.values())
        return target in available or any(target in p.output_types for p in chosen.values())

    @staticmethod
    def _pick(kind, target, providers, exclude, prefer=None) -> Provider | None:
        """First provider (in the deterministic order) that offers ``target``.

        ``prefer`` forces a particular provider for a particular goal, which is
        how alternative candidates are generated: same goals, different hands
        (spec §6).  It never relaxes ``exclude``.
        """
        wanted = (prefer or {}).get((kind, target))
        for provider in providers:
            if provider.key in exclude:
                continue
            offered = provider.capabilities if kind == "capability" else provider.output_types
            if target not in offered:
                continue
            if wanted is not None and provider.key != wanted:
                continue
            return provider
        return None

    @staticmethod
    def _offers(kind, target, providers, exclude) -> list[Provider]:
        """Every provider that could serve one goal, in the deterministic order."""
        return [
            p
            for p in providers
            if p.key not in exclude
            and target in (p.capabilities if kind == "capability" else p.output_types)
        ]

    def _layout(
        self,
        providers,
        requirements: list,
        available: list[str],
        required_outputs: list[str],
    ) -> PlanCandidate | None:
        """Order the chosen providers by data flow, building nodes and edges.

        A provider can be placed once everything it needs is either available
        from the start or produced by something already placed.  Failing to
        place them all means the data does not actually flow, however well the
        capabilities lined up (spec §19).
        """
        initial = [Port.parse(t) for t in available]
        remaining = list(providers)
        #: Every port produced so far, with the node that produces it.
        supply: list[tuple[Port, PlanNode]] = []
        nodes: list[PlanNode] = []
        edges: list[PlanEdge] = []
        reasons: list[str] = []
        ambiguous: list[dict] = []
        depth = 0

        while remaining:
            layer = [p for p in remaining if self._feedable(p, initial, supply)]
            if not layer:
                return None  # the remaining providers cannot be fed

            placed: list[tuple[Provider, PlanNode]] = []
            for provider in layer:
                node = PlanNode(
                    plan_id=_PLACEHOLDER,
                    node_key=f"{provider.key[0]}:v{provider.key[1]}",
                    definition_name=provider.key[0],
                    definition_version=provider.key[1],
                    provided_capabilities=list(provider.capabilities),
                    input_types=[str(p) for p in provider.input_ports],
                    output_types=[str(p) for p in provider.output_ports],
                    depth=depth,
                )
                nodes.append(node)
                placed.append((provider, node))

                for port in provider.input_ports:
                    sources = [(p, n) for p, n in supply if port.matches(p)]
                    if not sources and any(port.matches(p) for p in initial):
                        continue  # supplied from the plan's initial inputs
                    if len(sources) > 1:
                        # Two upstream nodes could feed this input and nothing
                        # says which.  Leaving it unbound is the whole point of
                        # 4B.1: picking one would be an unexplainable decision
                        # (Invariant 68).  The candidate still comes back, so
                        # the refusal can say what was ambiguous and between
                        # which producers.
                        ambiguous.append(
                            {
                                "node": node.node_key,
                                "port": str(port),
                                "sources": sorted(n.node_key for _, n in sources),
                            }
                        )
                        continue
                    if not sources:
                        return None  # nothing supplies it at all
                    source_port, source_node = sources[0]
                    edges.append(
                        PlanEdge(
                            plan_id=_PLACEHOLDER,
                            from_node_id=source_node.id,
                            to_node_id=node.id,
                            output_type=source_port.type,
                            output_key=source_port.key,
                            input_type=port.type,
                            input_key=port.key,
                            artifact_type=port.type,
                        )
                    )

            for provider, node in placed:
                supply.extend((port, node) for port in provider.output_ports)
                remaining.remove(provider)
            depth += 1

        covered = _unique(c for n in nodes for c in n.provided_capabilities)
        produced_types = _unique(port.type for port, _ in supply)
        for problem in ambiguous:
            reasons.append(
                f"ambiguous binding for {problem['node']}.{problem['port']}: "
                f"{problem['sources']}"
            )
        reasons.append(f"composed {len(nodes)} process(es) in {depth} stage(s)")
        return PlanCandidate(
            nodes=nodes,
            edges=edges,
            covered_capabilities=[r.name for r in requirements if r.name in covered],
            missing_capabilities=[r.name for r in requirements if r.name not in covered],
            produced_output_types=produced_types,
            missing_output_types=[t for t in required_outputs if t not in produced_types],
            ambiguous_bindings=ambiguous,
            reasons=reasons,
        )

    @staticmethod
    def _feedable(provider: Provider, initial: list[Port], supply: list) -> bool:
        """Whether every input port of ``provider`` can be supplied now."""
        return all(
            any(port.matches(p) for p in initial)
            or any(port.matches(p) for p, _ in supply)
            for port in provider.input_ports
        )

    def _alternatives(
        self, providers, requirements, available, required_outputs, best
    ) -> list[PlanCandidate]:
        """Enumerate structurally different plans, for comparison.

        Two complementary moves, both bounded and both deterministic:

        * **avoid** one of the chosen providers, which finds a plan built a
          different way round;
        * **force** a specific provider for one goal, which finds the plan that
          differs only in who does that step — the comparison a preference for
          cost, latency or risk is usually actually about (spec §6).

        Not an exhaustive search (spec §6 says it need not be): combinations of
        substitutions are not explored, because the number of them is the
        product of the alternatives and the value of them is small.
        """
        if best is None:
            return []
        budget = max(self.bounds.max_candidate_plans - 1, 0)
        seen = {candidate_fingerprint(best)}
        alternatives: list[PlanCandidate] = []

        def offer(candidate, reason: str) -> bool:
            """Keep a candidate if it is a shape we have not already got."""
            if candidate is None:
                return False
            fingerprint = candidate_fingerprint(candidate)
            if fingerprint in seen:
                return False
            seen.add(fingerprint)
            candidate.reasons.append(reason)
            alternatives.append(candidate)
            return len(alternatives) >= budget

        for node in best.nodes:
            if offer(
                self._compose(
                    providers, requirements, available, required_outputs,
                    exclude={node.key},
                ),
                f"alternative avoiding {node.node_key}",
            ):
                return alternatives

        for kind, target in self._substitutable_goals(requirements, required_outputs, providers):
            for provider in self._offers(kind, target, providers, set()):
                if offer(
                    self._compose(
                        providers, requirements, available, required_outputs,
                        prefer={(kind, target): provider.key},
                    ),
                    f"alternative using {provider.key[0]}:v{provider.key[1]} for {target}",
                ):
                    return alternatives
        return alternatives

    def _substitutable_goals(self, requirements, required_outputs, providers) -> list[tuple]:
        """Goals more than one provider can serve — the only ones worth swapping."""
        goals = [("capability", r.name) for r in requirements]
        goals += [("type", t) for t in required_outputs]
        return [
            goal
            for goal in _unique_pairs(goals)
            if len(self._offers(goal[0], goal[1], providers, set())) > 1
        ]

    # --- ranking -----------------------------------------------------------

    def _rank(self, candidate: PlanCandidate):
        """Total order over candidates (spec §27).

        Coverage first, then the simplest plan that achieves it: fewer
        processes, then fewer stages.  Priority breaks what is left, and the
        node keys break the rest — so the same inputs always yield the same
        plan, on any machine and after any restart.
        """
        return (
            not candidate.missing_capabilities,
            not candidate.missing_output_types,
            -candidate.node_count,
            -candidate.depth,
            self._priority_sum(candidate),
            _reverse(tuple(sorted(n.node_key for n in candidate.nodes))),
        )

    def _priority_sum(self, candidate: PlanCandidate) -> int:
        total = 0
        for node in candidate.nodes:
            definition = self._definition_cache.get(node.key)
            if definition is not None:
                total += definition_priority(definition)
        return total

    def score_candidates(self, candidates: list[PlanCandidate], definitions: list) -> None:
        """Attach a numeric score to each candidate (audit readability)."""
        self._definition_cache = {(d.name, d.version): d for d in definitions}
        for candidate in candidates:
            candidate.score = (
                100.0 * (not candidate.missing_capabilities)
                + 50.0 * (not candidate.missing_output_types)
                - candidate.node_count
                - candidate.depth
                + self._priority_sum(candidate)
            )


import uuid  # noqa: E402 - used only for the placeholder below

#: Nodes and edges are built before the plan they belong to has an id; the
#: composing process rewrites these when it persists the chosen candidate.
_PLACEHOLDER = uuid.UUID(int=0)


def _unique(values) -> list:
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(values))


def _unique_ports(ports) -> list[Port]:
    """Order-preserving de-duplication of ports."""
    return list(dict.fromkeys(ports))


def _unique_pairs(pairs) -> list:
    return list(dict.fromkeys(pairs))


class _reverse:
    """Sort helper: ascending order inside a descending sort."""

    __slots__ = ("value",)

    def __init__(self, value) -> None:
        self.value = value

    def __lt__(self, other: "_reverse") -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _reverse) and self.value == other.value
