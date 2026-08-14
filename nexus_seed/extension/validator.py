"""ExtensionValidator — nothing proceeds merely because something proposed it.

The same boundary this system draws around every proposal, applied to the one
thing that would eventually change the system itself.  What can go wrong here is
particular, so the checks are particular:

* a strategy nobody implements (spec §102) — or one the analyzer did not find,
  which is a proposal about a system that does not exist (spec §103);
* a target capability that is not in the gap (spec §104) — quietly widening what
  is being acquired;
* a component type nothing knows how to build (spec §113);
* a permission the strategy needs but the proposal does not declare — the Phase
  3C under-declaration failure, which here would understate the risk;
* a component that requires the very capability it is meant to provide;
* a proposal that would not close the gap, or that duplicates something already
  available (spec §37);
* a proposal that asks to change the core primitives or the policy judging it
  (spec §38, §79).

Every check is symbolic.  None of them reads the rationale: an explanation is
audit material, never evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import ComponentType, ExtensionProposal, ExtensionStrategy
from .strategies import (
    CRITICAL_PERMISSIONS,
    REUSE_STRATEGIES,
    implied_permissions,
    touches_core,
)


@dataclass
class ExtensionValidation:
    """Whether a proposal may go on to risk classification, and why not if not."""

    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    #: Everything the strategy and components would actually need.
    implied_permissions: list[str] = field(default_factory=list)
    #: Whether the proposal asks to change the core or the policy (spec §38).
    requests_core_change: bool = False

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)

    def note(self, reason: str) -> None:
        """Record something worth seeing that is not itself a failure."""
        self.reasons.append(reason)


class ExtensionValidator:
    """Checks an extension proposal against the gap and the current world."""

    def __init__(self, registry=None, *, matcher=None) -> None:
        self.registry = registry
        #: Asked "can we do this *right now*?" — a different question from the
        #: registry's "does a provider exist on paper?" (spec §71).
        self.matcher = matcher

    def validate(
        self,
        proposal: ExtensionProposal,
        *,
        gap,
        candidates=(),
        policy=None,
        definitions=(),
    ) -> ExtensionValidation:
        """Validate one proposal.

        Args:
            proposal: The proposal to check (possibly model-written).
            gap: The :class:`~nexus_seed.extension.models.CapabilityGap` it
                claims to close.
            candidates: The routes the analyzer found — the set a strategy had
                to come from.
            policy: The :class:`~nexus_seed.extension.policy.ExtensionPolicy`,
                which may forbid strategies outright.
            definitions: Every registered ProcessDefinition, for the
                "is this already available?" check.
        """
        result = ExtensionValidation()

        self._check_gap(proposal, gap, result)
        strategy = self._check_strategy(proposal, candidates, policy, result)
        self._check_targets(proposal, gap, result)
        self._check_components(proposal, result)
        self._check_permissions(proposal, strategy, result)
        self._check_self_reference(proposal, result)
        self._check_closes_gap(proposal, strategy, result)
        self._check_not_already_available(proposal, definitions, result)
        self._check_boundary(proposal, result)
        return result

    # --- individual checks --------------------------------------------------

    @staticmethod
    def _check_gap(proposal, gap, result) -> None:
        if gap is None:
            result.fail("the capability gap this proposal refers to does not exist")
            return
        if proposal.capability_gap_id != gap.id:
            result.fail(
                f"proposal names gap {proposal.capability_gap_id}, "
                f"validated against {gap.id}"
            )
        if getattr(gap.status, "terminal", False):
            result.fail(f"gap {gap.id} is already {gap.status.value}")

    @staticmethod
    def _check_strategy(proposal, candidates, policy, result) -> ExtensionStrategy | None:
        """The strategy must be known, offered, and permitted."""
        strategy = proposal.strategy
        if strategy is None:
            result.fail(
                f"strategy {proposal.declared_strategy!r} is not a known "
                "extension strategy"
            )
            return None
        if strategy is ExtensionStrategy.UNSUPPORTED:
            result.fail("UNSUPPORTED is not a strategy a proposal may take")
            return strategy

        offered = {c.strategy for c in candidates or ()}
        if offered and strategy not in offered:
            # The hallucination case, and the one that must never be a matter
            # of degree: a route the analyzer did not find is not a bolder
            # option, it is one nothing here can carry out (spec §103).
            result.fail(
                f"strategy {strategy.value} is not among the analyzed candidates "
                f"({sorted(s.value for s in offered)})"
            )
        if policy is not None and not policy.allows(strategy):
            result.fail(f"strategy {strategy.value} is not permitted by policy")
        return strategy

    @staticmethod
    def _check_targets(proposal, gap, result) -> None:
        """What is being acquired must be what is missing (spec §74, §104)."""
        if not proposal.target_capabilities:
            result.fail("proposal names no target capability")
            return
        if gap is None:
            return
        missing = set(gap.missing_names)
        outside = [name for name in proposal.target_names if name not in missing]
        if outside:
            result.fail(
                f"target capabilities {sorted(outside)} are not missing in this gap "
                f"({sorted(missing)})"
            )

    @staticmethod
    def _check_components(proposal, result) -> None:
        """Every component must be a kind of thing this system understands."""
        for component in proposal.proposed_components:
            if not ComponentType.known(component.component_type):
                result.fail(
                    f"component {component.name!r} has unknown component_type "
                    f"{component.component_type!r}"
                )
            if not component.name:
                result.fail("a proposed component has no name")

    @staticmethod
    def _check_permissions(proposal, strategy, result) -> None:
        """A proposal may not understate what building it would need.

        The Phase 3C rule (spec §17) in a new place: declaring fewer
        permissions than the work requires would lower the measured risk of the
        proposal without lowering the risk of the thing proposed.
        """
        needed = implied_permissions(strategy, proposal.proposed_components)
        result.implied_permissions = needed
        declared = set(proposal.required_permissions)
        undeclared = [p for p in needed if p not in declared]
        if undeclared:
            result.fail(
                f"proposal does not declare the permission(s) its construction "
                f"would need: {sorted(undeclared)}"
            )

    @staticmethod
    def _check_self_reference(proposal, result) -> None:
        """A component may not depend on the capability it exists to provide.

        A circular acquisition would be a proposal that can only be built once
        it is built — a real possibility once a model writes the decomposition.
        """
        targets = set(proposal.target_names)
        for component in proposal.proposed_components:
            provides = set(component.provides_capabilities)
            requires = set(component.requires_capabilities)
            overlap = provides & requires
            if overlap:
                result.fail(
                    f"component {component.name!r} requires {sorted(overlap)}, "
                    "which it is itself meant to provide"
                )
            circular = requires & targets
            if circular:
                result.fail(
                    f"component {component.name!r} requires {sorted(circular)}, "
                    "which this gap is missing"
                )
            outside = provides - targets
            if outside:
                result.fail(
                    f"component {component.name!r} claims to provide "
                    f"{sorted(outside)}, which is outside this gap"
                )

    @staticmethod
    def _check_closes_gap(proposal, strategy, result) -> None:
        """The proposal must plausibly close what it says it closes (spec §37).

        For a reuse strategy the covering thing is what already exists; for
        every other strategy it has to be one of the components being added.
        A proposal that adds nothing and reuses nothing describes no extension.
        """
        if strategy is None:
            return
        targets = set(proposal.target_names)
        if strategy in REUSE_STRATEGIES:
            if not proposal.reusable_components and not proposal.proposed_components:
                result.fail(
                    f"{strategy.value} names nothing to reuse and nothing to add"
                )
            return
        covered: set[str] = set()
        for component in proposal.proposed_components:
            covered.update(component.provides_capabilities)
        uncovered = targets - covered
        if uncovered:
            result.fail(
                f"no proposed component would provide {sorted(uncovered)}"
            )

    def _check_not_already_available(self, proposal, definitions, result) -> None:
        """Refuse a proposal to acquire something already usable (spec §37).

        The world moves while a proposal is being written: a capability
        registered in the meantime makes this extension unnecessary, and
        building it anyway would be the system extending itself for no reason.

        "Usable" is the matcher's sense, not the registry's — a capability
        whose only provider is disabled is exactly what a reuse proposal is
        *for*, and refusing that as "already available" would reject the best
        route this phase has.
        """
        already = [
            r.name for r in proposal.target_capabilities if self._provided(r, definitions)
        ]
        if already and len(already) == len(proposal.target_capabilities):
            result.fail(
                f"every target capability {sorted(already)} is already provided; "
                "no extension is needed"
            )
        elif already:
            result.note(f"{sorted(already)} became available while this was proposed")

    def _provided(self, requirement, definitions) -> bool:
        """Whether ``requirement`` can be met right now, by anything enabled."""
        if self.matcher is not None:
            return self.matcher.provides(requirement, list(definitions or ()))
        if self.registry is not None:
            return self.registry.is_provided(requirement)
        return False

    @staticmethod
    def _check_boundary(proposal, result) -> None:
        """Phase 5A's outer edge: the core and the policy are not extensible.

        Not a refusal of the *idea* — a seventh primitive might one day be
        right — but of deciding it here (spec §38).  A proposal that would
        change the runtime core, or relax the policy that is judging it, is
        recorded as such and left to a human (Invariant 91).
        """
        if touches_core(proposal.proposed_components):
            result.requests_core_change = True
            result.note(
                "proposal declares that it would modify the runtime core or the "
                "extension policy; that is never decided automatically"
            )
        critical = sorted(set(proposal.required_permissions) & CRITICAL_PERMISSIONS)
        if critical:
            result.requests_core_change = True
            result.note(
                f"proposal requires {critical}, which would let an extension change "
                "the rules it is judged by"
            )


__all__ = ["ExtensionValidation", "ExtensionValidator"]
