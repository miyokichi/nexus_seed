"""Capability models — what NEXUS SEED can do, as opposed to what it knows.

Until now, work found its implementation by *name*: a ``work_type`` was looked
up in a hard-coded table.  That works exactly as long as somebody has written
the table down in advance.  Phase 4A replaces the lookup with a question::

    what does this work need?    ->  required capabilities
    who can provide that?        ->  the capability registry
    is one such process here?    ->  the matcher

The consequence that matters is what happens when the answer is *no*.  A name
lookup that misses is a bug; a capability that nothing provides is a **fact
about the system** — the need is real and still stands, and it is recorded as
such rather than cancelled (Invariant 51).

Two things called "capability" must not be confused:

* :class:`~nexus_seed.backends.action.BackendCapabilities` (Phase 3C) — what an
  external mechanism can mechanically *do*: ``write_file``, ``read_file``.
* :class:`Capability` (here) — what a Process can meaningfully *accomplish*:
  ``analyze_resistance``, ``generate_analysis_report``.

The first is a tool; the second is a competence.  They are deliberately kept in
different registries (spec §4).

Nothing here is a core primitive: these are domain/registry data, like
``world/``, ``work/``, ``actions/``, ``ingress/`` and ``resources/`` before them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.event import utcnow

#: Version used when a declaration does not name one.
DEFAULT_VERSION = "1"


@dataclass(frozen=True)
class CapabilityRef:
    """A declaration that a ProcessDefinition provides some capability.

    Deliberately tiny: a process says *what it can do*, not how the registry
    should store it.
    """

    name: str
    version: str = DEFAULT_VERSION

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name}:v{self.version}"


@dataclass
class Capability:
    """A competence the system can offer, independent of who provides it.

    Attributes:
        name / version: Logical identity — unique together, so
            ``analyze_resistance:v1`` and ``:v2`` coexist as distinct
            capabilities rather than one overwriting the other.
        description: For humans (and, later, for planning).  **Not used for
            matching in Phase 4A** — matching is exact and deterministic.
        input_types / output_types: Recorded now, used for composition later
            (Phase 4B).  Phase 4A treats them as metadata.
        tags: Recorded, not used for eligibility.
        enabled: A disabled capability is invisible to matching but keeps its
            history — removal is a flag, not a delete (spec §42).
    """

    name: str
    version: str = DEFAULT_VERSION
    description: str | None = None
    input_types: list[str] = field(default_factory=list)
    output_types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    enabled: bool = True
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def key(self) -> tuple[str, str]:
        """The ``(name, version)`` pair this capability is unique by."""
        return (self.name, self.version)

    @property
    def ref(self) -> CapabilityRef:
        """This capability as a declaration reference."""
        return CapabilityRef(self.name, self.version)


@dataclass
class CapabilityRequirement:
    """A capability a unit of work needs in order to be doable.

    Attributes:
        name: The capability required.
        version_constraint: An exact version, or ``None`` for "any".  Phase 4A
            deliberately stops there — a SemVer solver would be machinery
            without a problem yet (spec §14).
        required: ``False`` marks a nice-to-have: it can inform ranking but
            never decides eligibility (spec §22).
    """

    name: str
    version_constraint: str | None = None
    required: bool = True
    metadata: dict = field(default_factory=dict)

    def satisfied_by(self, capability: Capability) -> bool:
        """Whether ``capability`` meets this requirement."""
        if capability.name != self.name or not capability.enabled:
            return False
        return (
            self.version_constraint is None
            or capability.version == self.version_constraint
        )

    def to_dict(self) -> dict:
        """Serialize for the ``required_capabilities_json`` column."""
        return {
            "name": self.name,
            "version_constraint": self.version_constraint,
            "required": self.required,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityRequirement":
        """Rebuild from :meth:`to_dict` output."""
        return cls(
            name=data.get("name", ""),
            version_constraint=data.get("version_constraint"),
            required=data.get("required", True),
            metadata=data.get("metadata") or {},
        )

    @classmethod
    def coerce(cls, value) -> "CapabilityRequirement":
        """Accept a bare name, a dict, or an instance."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(name=value)
        return cls.from_dict(value or {})


class CapabilityMatchStatus(str, Enum):
    """How a capability match turned out.

    The three outcomes are deliberately distinct because they call for
    different responses (spec §92):

    * ``MATCHED_SINGLE_PROCESS`` — go.
    * ``MISSING_CAPABILITY`` — nothing in the system provides some requirement.
      This is the input to self-extension, later: *we need something we do not
      have*.
    * ``COMPOSITION_REQUIRED`` — every requirement has a provider, but no single
      process covers them all.  Phase 4A stops here on purpose; combining them
      is Phase 4B (Invariant 54).
    """

    MATCHED_SINGLE_PROCESS = "MATCHED_SINGLE_PROCESS"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    MISSING_PROVIDER = "MISSING_PROVIDER"
    COMPOSITION_REQUIRED = "COMPOSITION_REQUIRED"

    @property
    def eligible(self) -> bool:
        """Whether a process may be spawned for this outcome."""
        return self is CapabilityMatchStatus.MATCHED_SINGLE_PROCESS


@dataclass
class CandidateMatch:
    """One ProcessDefinition weighed against a work requirement's needs.

    Attributes:
        covered_capabilities / missing_capabilities: What this candidate can and
            cannot do out of what was asked.
        score: Deterministic rank; higher wins.  Never a learned or fuzzy score.
        eligible: Covers every *required* capability on its own.
        reasons: Why it was or was not chosen — the audit trail.
    """

    definition_name: str
    definition_version: str
    covered_capabilities: list[str] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    optional_covered: list[str] = field(default_factory=list)
    score: float = 0.0
    eligible: bool = False
    provider_available: bool = True
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        """The ``(name, version)`` of the candidate definition."""
        return (self.definition_name, self.definition_version)

    def to_dict(self) -> dict:
        """Serialize for the match audit record."""
        return {
            "definition_name": self.definition_name,
            "definition_version": self.definition_version,
            "covered": list(self.covered_capabilities),
            "missing": list(self.missing_capabilities),
            "optional_covered": list(self.optional_covered),
            "score": self.score,
            "eligible": self.eligible,
            "provider_available": self.provider_available,
            "reasons": list(self.reasons),
        }


@dataclass
class MatchResult:
    """The outcome of matching one work requirement against the registry."""

    status: CapabilityMatchStatus
    selected: CandidateMatch | None = None
    candidates: list[CandidateMatch] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        """Whether a process was found that can do the whole job alone."""
        return self.selected is not None and self.status.eligible


@dataclass
class CapabilityWorkMatch:
    """A durable record of one matching attempt (spec §47).

    Never overwritten.  A requirement blocked today and matched tomorrow keeps
    both attempts, so "why did nothing happen for two days?" is answerable
    (spec §48).
    """

    work_requirement_id: uuid.UUID
    status: CapabilityMatchStatus
    required_capabilities: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    selected_definition_name: str | None = None
    selected_definition_version: str | None = None
    reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    @classmethod
    def from_result(
        cls,
        work_requirement_id: uuid.UUID,
        requirements: list[CapabilityRequirement],
        result: MatchResult,
    ) -> "CapabilityWorkMatch":
        """Build an audit record from a matching outcome."""
        return cls(
            work_requirement_id=work_requirement_id,
            status=result.status,
            required_capabilities=[r.to_dict() for r in requirements],
            candidates=[c.to_dict() for c in result.candidates],
            missing_capabilities=list(result.missing_capabilities),
            selected_definition_name=(
                result.selected.definition_name if result.selected else None
            ),
            selected_definition_version=(
                result.selected.definition_version if result.selected else None
            ),
            reasons=list(result.reasons),
        )
