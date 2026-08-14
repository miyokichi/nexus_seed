"""CapabilityMatcher — which single process can do this whole job?

Entirely deterministic (spec §23, §26).  No LLM, no embeddings, no similarity.
That is not a limitation to be lifted later so much as the point of this phase:
before anything can *reason* about which process suits a job, the system needs
an exact, reproducible answer to whether one exists at all — and an audit trail
saying why it chose what it chose.

Phase 4A answers with one process or none (Invariant 53).  When every required
capability exists but no single process covers them all, the result is
``COMPOSITION_REQUIRED`` — recorded, not solved.  Combining processes is
Phase 4B, and pretending otherwise here would smuggle in a planner.
"""

from __future__ import annotations

import logging

from .models import (
    CandidateMatch,
    CapabilityMatchStatus,
    CapabilityRequirement,
    MatchResult,
)
from .registry import version_sort_key

logger = logging.getLogger("nexus_seed.capabilities.matcher")

#: Metadata key a definition uses to bid for selection; higher wins.
PRIORITY_METADATA_KEY = "capability_priority"

#: Metadata key marking a definition unavailable for new matching.
ENABLED_METADATA_KEY = "enabled"


def definition_priority(definition) -> int:
    """Return a definition's selection priority (default 0, higher wins)."""
    raw = (definition.metadata or {}).get(PRIORITY_METADATA_KEY, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def definition_enabled(definition) -> bool:
    """Whether a definition may be selected for new work (spec §41)."""
    return bool((definition.metadata or {}).get(ENABLED_METADATA_KEY, True))


class CapabilityMatcher:
    """Chooses the one ProcessDefinition that can satisfy a work requirement."""

    def __init__(self, registry) -> None:
        self.registry = registry

    def match(
        self,
        requirements: list[CapabilityRequirement],
        definitions: list,
    ) -> MatchResult:
        """Weigh every definition against ``requirements`` and pick one.

        Args:
            requirements: What the work needs.
            definitions: The ProcessDefinitions available to consider.
        """
        mandatory = [r for r in requirements if r.required]
        optional = [r for r in requirements if not r.required]

        evaluated = [
            self._evaluate(definition, mandatory, optional)
            for definition in definitions
            if definition_enabled(definition)
        ]
        # A definition providing none of what was asked was never in the
        # running; recording it would make the audit grow with the size of the
        # system rather than the size of the decision.  (With no requirements
        # at all, everything trivially qualifies.)
        candidates = [
            c
            for c in evaluated
            if not mandatory or c.covered_capabilities or c.optional_covered
        ]
        candidates.sort(key=self._rank, reverse=True)

        eligible = [c for c in candidates if c.eligible]
        if eligible:
            selected = eligible[0]
            selected.reasons.append("selected: highest-ranked eligible candidate")
            return MatchResult(
                status=CapabilityMatchStatus.MATCHED_SINGLE_PROCESS,
                selected=selected,
                candidates=candidates,
                reasons=[
                    f"{len(eligible)} eligible candidate(s); "
                    f"chose {selected.definition_name} v{selected.definition_version}"
                ],
            )

        # Nothing can do the whole job.  *Why* not is the useful part.
        # Derived from the candidates actually considered, so a capability
        # whose only provider is a disabled definition counts as unprovided —
        # which is what an operator (and, later, self-extension) needs to know.
        unprovided = [
            r.name
            for r in mandatory
            if not any(r.name in c.covered_capabilities for c in candidates)
        ]
        if unprovided:
            return MatchResult(
                status=CapabilityMatchStatus.MISSING_CAPABILITY,
                candidates=candidates,
                missing_capabilities=unprovided,
                reasons=[f"no process provides {sorted(set(unprovided))}"],
            )

        # Every capability exists somewhere — just not together.
        return MatchResult(
            status=CapabilityMatchStatus.COMPOSITION_REQUIRED,
            candidates=candidates,
            missing_capabilities=[],
            reasons=[
                "every required capability is provided, but no single process "
                "covers them all (composition is Phase 4B)"
            ],
        )

    # --- per-candidate evaluation -----------------------------------------

    def _evaluate(
        self,
        definition,
        mandatory: list[CapabilityRequirement],
        optional: list[CapabilityRequirement],
    ) -> CandidateMatch:
        provided = self.registry.get_capabilities_for_process(
            definition.name, definition.version
        )
        covered, missing = self._split(mandatory, provided)
        optional_covered, _ = self._split(optional, provided)

        candidate = CandidateMatch(
            definition_name=definition.name,
            definition_version=definition.version,
            covered_capabilities=covered,
            missing_capabilities=missing,
            optional_covered=optional_covered,
            eligible=not missing,
        )
        candidate.score = self._score(definition, candidate)
        if missing:
            candidate.reasons.append(f"does not provide {sorted(missing)}")
        else:
            candidate.reasons.append(f"provides all of {sorted(covered)}")
            if optional_covered:
                candidate.reasons.append(f"also provides optional {sorted(optional_covered)}")
        return candidate

    @staticmethod
    def _split(
        requirements: list[CapabilityRequirement], provided: list
    ) -> tuple[list[str], list[str]]:
        covered, missing = [], []
        for requirement in requirements:
            if any(requirement.satisfied_by(c) for c in provided):
                covered.append(requirement.name)
            else:
                missing.append(requirement.name)
        return covered, missing

    @staticmethod
    def _score(definition, candidate: CandidateMatch) -> float:
        """Deterministic score; only meaningful relative to other candidates."""
        return (
            (100.0 if candidate.eligible else 0.0)
            + definition_priority(definition)
            + len(candidate.optional_covered)
        )

    @staticmethod
    def _rank(candidate: CandidateMatch):
        """Total order over candidates, so selection never depends on luck.

        Eligibility, then score, then newest definition version, then name —
        the last two exist purely to break ties the same way every time, on
        every machine and after every restart (spec §24).
        """
        return (
            candidate.eligible,
            candidate.score,
            version_sort_key(candidate.definition_version),
            _reverse_name(candidate.definition_name),
        )


class _reverse_name:
    """Sort helper: makes name ordering ascending inside a reversed sort."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: "_reverse_name") -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _reverse_name) and self.value == other.value
