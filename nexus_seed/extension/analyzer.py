"""CapabilityAcquisitionAnalyzer — what could close this gap, before anyone asks.

Deterministic, like the capability matcher and the composition planner before
it (spec §25).  No model is consulted here, and that is not a staging decision:
*"how could I acquire this?"* asked of a language model with nothing else in
front of it will answer "write some code" almost every time, because writing
code is the answer that always applies.  Discovering that the system already
has a disabled process which does exactly this requires looking, and looking is
what this module does.

What it looks at (spec §23):

    the capability registry        who provides this, and are they enabled?
    the process definitions        who could provide it if configured?
    the registered backends        do the mechanical parts already exist?
    the extractor registry         can we already read that format?
    the plugin catalog             is there a known external candidate?

What it produces is :class:`~nexus_seed.extension.models.AcquisitionCandidate`
objects in reuse-first order (Invariant 88), which is also the set of strategies
a model is later allowed to choose between (spec §35).  It produces no proposal,
writes nothing, and registers nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..capabilities.matcher import definition_enabled
from ..capabilities.models import CapabilityRequirement
from .models import (
    AcquisitionCandidate,
    AcquisitionFeasibility,
    ComponentType,
    ExtensionStrategy,
    ProposedComponent,
)
from .strategies import (
    STRATEGY_COST,
    STRATEGY_RISK,
    classify_risk,
    implied_permissions,
    strategy_rank,
)

logger = logging.getLogger("nexus_seed.extension.analyzer")

#: Where a capability (or the work needing it) describes what acquiring it
#: would involve.  Deliberately a declaration, not an inference: Phase 5A does
#: no semantic aliasing (spec §75), so "this capability is an extraction of
#: PowerPoint files" has to be *said* somewhere rather than guessed from a name.
EXTENSION_HINTS_KEY = "extension"

#: Metadata key on a ProcessDefinition listing capabilities it could provide
#: under a different configuration (spec §17, step 2).
CONFIGURABLE_METADATA_KEY = "configurable_capabilities"


@dataclass
class PluginCatalog:
    """Known external plugins, as static data (spec §141).

    A fixture, not a marketplace client: Phase 5A performs no network
    discovery, and a plugin here is a *candidate to describe*, never something
    that gets installed (spec §29, §140).
    """

    entries: dict[str, dict] = field(default_factory=dict)

    def register(self, capability_name: str, **descriptor) -> dict:
        """Record that ``capability_name`` is offered by a known plugin."""
        entry = {"name": descriptor.get("name", capability_name), **descriptor}
        self.entries[capability_name] = entry
        return entry

    def get(self, capability_name: str) -> dict | None:
        return self.entries.get(capability_name)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.entries)


@dataclass
class AcquisitionEnvironment:
    """Everything the analyzer is allowed to look at.

    Passed in rather than reached for, so an analysis can be reproduced from
    its inputs and a test can describe a world in three lines.
    """

    definitions: list = field(default_factory=list)
    backends: dict = field(default_factory=dict)
    extractors: object | None = None
    adapters: object | None = None
    plugins: PluginCatalog | None = None

    def backend_supporting(self, actions) -> str | None:
        """The first registered backend supporting *every* action in ``actions``."""
        wanted = list(actions or ())
        if not wanted:
            return None
        for name in sorted(self.backends):
            capabilities = self.backends[name]
            if capabilities is None:
                continue
            if all(capabilities.supports(a) for a in wanted):
                return name
        return None

    def unsupported_actions(self, actions) -> list[str]:
        """Which of ``actions`` no registered backend can perform."""
        missing = []
        for action in actions or ():
            if not any(
                c is not None and c.supports(action) for c in self.backends.values()
            ):
                missing.append(action)
        return missing

    def has_extractor(self, representation_type: str, resource_type: str) -> bool:
        """Whether the extractor registry already covers that pair."""
        if self.extractors is None:
            return False
        return self.extractors.find(representation_type, resource_type) is not None

    def has_adapter(self, adapter_id: str) -> bool:
        """Whether an ingress adapter is registered under ``adapter_id``."""
        return bool(self.adapters is not None and adapter_id in self.adapters)


class CapabilityAcquisitionAnalyzer:
    """Works out which routes to a missing capability actually exist."""

    name = "deterministic"
    version = "1"

    def __init__(self, registry=None) -> None:
        self.registry = registry

    # --- entry point -------------------------------------------------------

    def analyze(
        self, gap, *, environment: AcquisitionEnvironment | None = None
    ) -> list[AcquisitionCandidate]:
        """Return every route that could close ``gap``, best (most reusing) first.

        One candidate per strategy: routes found for different missing
        capabilities that share a strategy are merged, so the caller sees a
        list of *ways to proceed* rather than a matrix.
        """
        env = environment or AcquisitionEnvironment()
        per_strategy: dict[ExtensionStrategy, AcquisitionCandidate] = {}

        for requirement in gap.missing_capabilities:
            for candidate in self._for_capability(requirement, env):
                existing = per_strategy.get(candidate.strategy)
                if existing is None:
                    per_strategy[candidate.strategy] = candidate
                else:
                    _merge(existing, candidate)

        candidates = sorted(per_strategy.values(), key=_order)
        for candidate in candidates:
            candidate.required_permissions = implied_permissions(
                candidate.strategy, candidate.required_new_components
            )
            candidate.estimated_risk = classify_risk(
                candidate.strategy,
                components=candidate.required_new_components,
                permissions=candidate.required_permissions,
            )
        logger.info(
            "gap %s: %d acquisition candidate(s) %s",
            gap.id,
            len(candidates),
            [c.strategy.value for c in candidates],
        )
        return candidates

    # --- one missing capability --------------------------------------------

    def _for_capability(
        self, requirement: CapabilityRequirement, env: AcquisitionEnvironment
    ) -> list[AcquisitionCandidate]:
        """Every route to one missing capability, in no particular order."""
        name = requirement.name
        hints = self._hints(requirement)
        if hints.get("unsupported") or hints.get("requires_core_change"):
            # No route exists that this architecture can express.  Saying so is
            # a better answer than inventing a plausible one (spec §65).
            return [
                _candidate(
                    ExtensionStrategy.UNSUPPORTED,
                    name,
                    feasibility=AcquisitionFeasibility.UNSUPPORTED,
                    reasons=[
                        f"{name} declares that acquiring it would require a change "
                        "this architecture cannot express"
                    ],
                )
            ]

        found: list[AcquisitionCandidate] = []
        found.extend(self._reuse_existing_process(name, env))
        found.extend(self._configure_existing_process(name, env))
        found.extend(self._backend_routes(name, hints, env))
        found.extend(self._resource_routes(name, hints, env))
        found.extend(self._adapter_routes(name, hints, env))
        found.extend(self._plugin_routes(name, hints, env))

        if not found:
            # Last resort, and only when it *is* the last resort (spec §73).
            found.append(
                _candidate(
                    ExtensionStrategy.CODE_EXTENSION,
                    name,
                    components=[
                        ProposedComponent(
                            component_type=ComponentType.CODE_MODULE.value,
                            name=hints.get("component_name") or f"{name}_module",
                            purpose=f"implement {name}",
                            provides_capabilities=[name],
                        )
                    ],
                    feasibility=AcquisitionFeasibility.UNKNOWN,
                    reasons=[
                        f"nothing registered provides or could be configured to "
                        f"provide {name}, and no adapter, extractor or plugin route "
                        f"was declared for it"
                    ],
                )
            )
        return found

    # --- individual routes --------------------------------------------------

    def _reuse_existing_process(
        self, name: str, env: AcquisitionEnvironment
    ) -> list[AcquisitionCandidate]:
        """A provider exists but is switched off — the cheapest possible route.

        This is the case that justifies the whole reuse-first ordering: the
        competence is already written, tested and declared, and the "extension"
        is a flag.  Missing it and proposing new code instead would be the
        system failing to know itself.
        """
        if self.registry is None:
            return []
        reusable: list[str] = []
        reasons: list[str] = []
        for capability in self._all_versions(name):
            for definition_name, definition_version in self._providers(capability):
                definition = self._definition(env, definition_name, definition_version)
                key = f"{definition_name}:{definition_version}"
                if definition is None:
                    continue
                if not capability.enabled:
                    reusable.append(key)
                    reasons.append(
                        f"{key} provides {name} but the capability is disabled"
                    )
                elif not definition_enabled(definition):
                    reusable.append(key)
                    reasons.append(f"{key} provides {name} but the process is disabled")
        if not reusable:
            return []
        return [
            _candidate(
                ExtensionStrategy.REGISTER_EXISTING_PROCESS,
                name,
                reusable=sorted(set(reusable)),
                feasibility=AcquisitionFeasibility.FEASIBLE,
                reasons=reasons,
            )
        ]

    def _configure_existing_process(
        self, name: str, env: AcquisitionEnvironment
    ) -> list[AcquisitionCandidate]:
        """A registered process says it could provide this under a setting."""
        reusable = []
        for definition in env.definitions:
            declared = (definition.metadata or {}).get(CONFIGURABLE_METADATA_KEY) or ()
            if name in declared:
                reusable.append(f"{definition.name}:{definition.version}")
        if not reusable:
            return []
        return [
            _candidate(
                ExtensionStrategy.CONFIGURE_EXISTING_PROCESS,
                name,
                reusable=sorted(reusable),
                components=[
                    ProposedComponent(
                        component_type=ComponentType.CONFIGURATION.value,
                        name=f"{name}_configuration",
                        purpose=f"configure an existing process to provide {name}",
                        provides_capabilities=[name],
                    )
                ],
                feasibility=AcquisitionFeasibility.FEASIBLE,
                reasons=[
                    f"{key} declares {name} among its configurable capabilities"
                    for key in sorted(reusable)
                ],
            )
        ]

    def _backend_routes(
        self, name: str, hints: dict, env: AcquisitionEnvironment
    ) -> list[AcquisitionCandidate]:
        """The mechanical half exists; the question is what is missing above it.

        This is where the two meanings of "capability" have to be held apart
        (spec §28).  ``write_file`` is a *tool* and ``modify_repository`` is a
        *competence*; having the first is not having the second, but it does
        mean the extension needed is a process, not a backend (spec §72).
        """
        actions = list(hints.get("backend_actions") or ())
        if not actions:
            return []
        unsupported = env.unsupported_actions(actions)
        if unsupported:
            # The mechanism itself is missing.  A new ActionBackend is real
            # code touching the outside world — HIGH, and described as such.
            return [
                _candidate(
                    ExtensionStrategy.CODE_EXTENSION,
                    name,
                    components=[
                        ProposedComponent(
                            component_type=ComponentType.ACTION_BACKEND.value,
                            name=hints.get("component_name") or f"{name}_backend",
                            purpose=f"perform {sorted(unsupported)} for {name}",
                            provides_capabilities=[name],
                            metadata={"action_types": sorted(unsupported)},
                        )
                    ],
                    feasibility=AcquisitionFeasibility.UNKNOWN,
                    reasons=[
                        f"no registered backend performs {sorted(unsupported)}",
                    ],
                )
            ]

        named = hints.get("backend")
        if named and named in env.backends and env.backend_supporting(actions) is not None:
            return [
                _candidate(
                    ExtensionStrategy.CONNECT_EXISTING_BACKEND,
                    name,
                    reusable=[f"backend:{named}"],
                    components=[
                        ProposedComponent(
                            component_type=ComponentType.CONFIGURATION.value,
                            name=f"{name}_backend_binding",
                            purpose=f"bind {name} to the registered {named} backend",
                            provides_capabilities=[name],
                            metadata={"backend": named, "action_types": sorted(actions)},
                        )
                    ],
                    feasibility=AcquisitionFeasibility.FEASIBLE,
                    reasons=[
                        f"backend {named!r} is registered and performs {sorted(actions)}"
                    ],
                )
            ]

        backend = env.backend_supporting(actions)
        return [
            _candidate(
                ExtensionStrategy.ADD_PROCESS_DEFINITION,
                name,
                reusable=[f"backend:{backend}"] if backend else [],
                components=[
                    ProposedComponent(
                        component_type=ComponentType.PROCESS_DEFINITION.value,
                        name=hints.get("component_name") or f"{name}_process",
                        purpose=(
                            f"provide {name} using the already available "
                            f"{sorted(actions)} backend action(s)"
                        ),
                        provides_capabilities=[name],
                        metadata={"backend": backend, "action_types": sorted(actions)},
                    )
                ],
                feasibility=AcquisitionFeasibility.FEASIBLE,
                reasons=[
                    f"the mechanical action(s) {sorted(actions)} already exist "
                    f"(backend {backend!r}); what is missing is a process that "
                    f"means {name}"
                ],
            )
        ]

    def _resource_routes(
        self, name: str, hints: dict, env: AcquisitionEnvironment
    ) -> list[AcquisitionCandidate]:
        """Reading a format we cannot read yet is an extractor, not an engine.

        Phase 3E made adding a format a registration (Invariant 38), so the
        extension this needs is small and local — which is exactly what the
        risk classification should say.
        """
        resource_type = hints.get("resource_type")
        if not resource_type:
            return []
        representation = hints.get("representation") or "structure"
        if env.has_extractor(representation, resource_type):
            return [
                _candidate(
                    ExtensionStrategy.ADD_PROCESS_DEFINITION,
                    name,
                    reusable=[f"extractor:{representation}/{resource_type}"],
                    components=[
                        ProposedComponent(
                            component_type=ComponentType.PROCESS_DEFINITION.value,
                            name=hints.get("component_name") or f"{name}_process",
                            purpose=(
                                f"provide {name} over the existing "
                                f"{resource_type} extractor"
                            ),
                            provides_capabilities=[name],
                            metadata={
                                "resource_type": resource_type,
                                "representation": representation,
                            },
                        )
                    ],
                    feasibility=AcquisitionFeasibility.FEASIBLE,
                    reasons=[
                        f"{resource_type} can already be extracted as "
                        f"{representation}; what is missing is a process that "
                        f"means {name}"
                    ],
                )
            ]
        return [
            _candidate(
                ExtensionStrategy.ADD_EXTRACTOR,
                name,
                reusable=["resources:ExtractorRegistry"],
                components=[
                    ProposedComponent(
                        component_type=ComponentType.RESOURCE_EXTRACTOR.value,
                        name=hints.get("component_name")
                        or f"{resource_type}_extractor",
                        purpose=f"render {resource_type} content as {representation}",
                        provides_capabilities=[name],
                        metadata={
                            "resource_type": resource_type,
                            "representation_type": representation,
                        },
                    )
                ],
                feasibility=AcquisitionFeasibility.FEASIBLE,
                reasons=[
                    f"the resource layer and extractor registry exist, but no "
                    f"extractor renders {resource_type} as {representation}"
                ],
            )
        ]

    def _adapter_routes(
        self, name: str, hints: dict, env: AcquisitionEnvironment
    ) -> list[AcquisitionCandidate]:
        """Observing a source nothing watches yet is an ingress adapter."""
        source = hints.get("ingress_source")
        if not source or env.has_adapter(source):
            return []
        return [
            _candidate(
                ExtensionStrategy.ADD_ADAPTER,
                name,
                reusable=["ingress:IngressService"],
                components=[
                    ProposedComponent(
                        component_type=ComponentType.INGRESS_ADAPTER.value,
                        name=hints.get("component_name") or f"{source}_adapter",
                        purpose=f"observe {source} through the ingress boundary",
                        provides_capabilities=[name],
                        metadata={"adapter_id": source},
                    )
                ],
                feasibility=AcquisitionFeasibility.FEASIBLE,
                reasons=[
                    f"the ingress boundary exists but no adapter observes {source!r}"
                ],
            )
        ]

    def _plugin_routes(
        self, name: str, hints: dict, env: AcquisitionEnvironment
    ) -> list[AcquisitionCandidate]:
        """A known external plugin offers this — described, never installed."""
        catalog = env.plugins
        if catalog is None:
            return []
        entry = catalog.get(hints.get("plugin") or name)
        if entry is None:
            return []
        return [
            _candidate(
                ExtensionStrategy.ADD_EXTERNAL_PLUGIN,
                name,
                components=[
                    ProposedComponent(
                        component_type=ComponentType.PLUGIN.value,
                        name=str(entry.get("name", name)),
                        purpose=f"obtain {name} from an external plugin",
                        provides_capabilities=[name],
                        metadata=dict(entry),
                    )
                ],
                feasibility=AcquisitionFeasibility.FEASIBLE,
                reasons=[
                    f"plugin {entry.get('name', name)!r} is listed as providing {name}; "
                    "Phase 5A describes it and installs nothing"
                ],
            )
        ]

    # --- registry helpers ---------------------------------------------------

    def _hints(self, requirement: CapabilityRequirement) -> dict:
        """What is declared about acquiring this capability.

        The work's own hint is the base and the registry's declaration
        overrides it: what the system says about its own competences outranks
        what a piece of work claims about them.
        """
        hints = dict((requirement.metadata or {}).get(EXTENSION_HINTS_KEY) or {})
        if self.registry is None:
            return hints
        # A *disabled* capability still describes how it would be acquired, and
        # ``get_capability`` only returns enabled ones — so fall back to the
        # full version list rather than losing the declaration.
        capability = self.registry.get_capability(requirement.name)
        if capability is None:
            versions = self._all_versions(requirement.name)
            capability = versions[0] if versions else None
        declared = (capability.metadata or {}).get(EXTENSION_HINTS_KEY) if capability else None
        if isinstance(declared, dict):
            hints.update(declared)
        return hints

    def _all_versions(self, name: str) -> list:
        """Every version of a capability, **including disabled ones**.

        Disabled versions are the interesting ones here: to the matcher a
        disabled capability does not exist, and to an analyzer looking for
        something to reuse it is the best news available.
        """
        store = getattr(self.registry, "store", None)
        if store is None:
            return []
        return store.versions_of(name, enabled_only=False)

    def _providers(self, capability) -> list[tuple[str, str]]:
        """Who declares this capability — **including for a disabled one**.

        ``providers_of`` hides disabled capabilities by default, which is right
        for matching and exactly wrong here: a disabled capability with a
        provider is the cheapest extension there is, and the default would make
        it invisible to the one component whose job is to find it.
        """
        store = getattr(self.registry, "store", None)
        if store is None:
            return []
        return store.providers_of(
            capability.name, capability.version, enabled_only=False
        )

    @staticmethod
    def _definition(env: AcquisitionEnvironment, name: str, version: str):
        for definition in env.definitions:
            if definition.name == name and definition.version == version:
                return definition
        return None


# --- helpers ----------------------------------------------------------------


def _candidate(
    strategy: ExtensionStrategy,
    capability: str,
    *,
    reusable=(),
    components=(),
    feasibility: AcquisitionFeasibility = AcquisitionFeasibility.UNKNOWN,
    reasons=(),
) -> AcquisitionCandidate:
    return AcquisitionCandidate(
        strategy=strategy,
        target_capabilities=[capability],
        reusable_components=list(reusable),
        required_new_components=list(components),
        estimated_risk=STRATEGY_RISK.get(strategy),
        estimated_cost=STRATEGY_COST.get(strategy),
        feasibility=feasibility,
        reasons=list(reasons),
    )


def _merge(into: AcquisitionCandidate, other: AcquisitionCandidate) -> None:
    """Fold a second route with the same strategy into the first."""
    for name in other.target_capabilities:
        if name not in into.target_capabilities:
            into.target_capabilities.append(name)
    for reusable in other.reusable_components:
        if reusable not in into.reusable_components:
            into.reusable_components.append(reusable)
    known = {c.name for c in into.required_new_components}
    for component in other.required_new_components:
        if component.name not in known:
            into.required_new_components.append(component)
    into.reasons.extend(other.reasons)
    if into.estimated_cost is not None and other.estimated_cost is not None:
        into.estimated_cost += other.estimated_cost
    if other.feasibility.rank > into.feasibility.rank:
        into.feasibility = other.feasibility


def _order(candidate: AcquisitionCandidate):
    """Total order over candidates — reuse first, then certainty, then cost.

    Total on purpose (spec §100): the last tie-break is the capability names,
    so the same registry and the same gap produce the same order on any machine
    and after any restart.
    """
    return (
        strategy_rank(candidate.strategy),
        candidate.feasibility.rank,
        candidate.estimated_risk.rank if candidate.estimated_risk else 3,
        candidate.estimated_cost if candidate.estimated_cost is not None else 0.0,
        ",".join(sorted(candidate.target_capabilities)),
    )


__all__ = [
    "CONFIGURABLE_METADATA_KEY",
    "EXTENSION_HINTS_KEY",
    "AcquisitionEnvironment",
    "CapabilityAcquisitionAnalyzer",
    "PluginCatalog",
]
