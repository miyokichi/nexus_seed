"""Self-extension domain data — what we cannot do, and how we might come to.

Phase 4A gave the system a way to say *we cannot do this*: a WorkRequirement
whose required capabilities nothing provides becomes ``BLOCKED_CAPABILITY``
rather than being cancelled (Invariant 51).  That statement was true and
completely inert.  Phase 5A makes it the beginning of a question:

    what exactly is missing?          ->  CapabilityGap
    could we reuse something we have? ->  AcquisitionCandidate
    what would acquiring it involve?  ->  ExtensionProposal
    may that proceed?                 ->  validation -> risk -> policy -> human

Two separations carry the whole phase.

**A need is not a deficiency.**  A :class:`~nexus_seed.work.work_requirement.
WorkRequirement` is something the world requires of us; a :class:`CapabilityGap`
is something we lack in ourselves (spec §10).  Keeping them apart is what stops
"we cannot do this" from being written back onto the need as though the need had
changed.

**A deficiency is not a permission** (Invariant 84).  Nothing here modifies a
repository, registers a capability, installs anything or grants a permission —
an :class:`ExtensionProposal` is a *description of an extension that would
close a gap*, and Phase 5A stops at deciding whether that description may be
handed on to a construction phase (Invariant 90).

Nothing in this module is a core primitive.  ``Extension``, ``Skill``,
``Plugin``, ``Builder`` and ``SelfModification`` are deliberately absent: these
are domain records produced by an ordinary Process, exactly as
``ActionProposal`` and ``PlanSelectionProposal`` already are.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..capabilities.models import CapabilityRequirement
from ..core.event import utcnow


# --- capability gap ---------------------------------------------------------


class CapabilityGapStatus(str, Enum):
    """Lifecycle of a :class:`CapabilityGap`.

    The distinction that matters is between :attr:`PROPOSAL_APPROVED` and
    :attr:`RESOLVED` (spec §62).  An approved proposal means *we have decided
    how we would acquire this*; it does not mean we can do it.  Collapsing the
    two would let the system believe it had gained a competence by agreeing
    that acquiring it was a good idea.
    """

    #: The gap stands and nothing is currently being proposed for it.
    OPEN = "OPEN"
    #: A (re)analysis has been requested; no decided proposal yet.
    PROPOSAL_PENDING = "PROPOSAL_PENDING"
    #: A validated proposal exists and is waiting for a decision.
    PROPOSAL_AVAILABLE = "PROPOSAL_AVAILABLE"
    #: A proposal was approved — for construction, not for use (spec §62).
    PROPOSAL_APPROVED = "PROPOSAL_APPROVED"
    #: The capability became available (by any route); the gap is closed.
    RESOLVED = "RESOLVED"
    #: The gap no longer matters (its work was cancelled).
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        """Whether this gap is finished with."""
        return self in (CapabilityGapStatus.RESOLVED, CapabilityGapStatus.CANCELLED)

    @classmethod
    def coerce(cls, value: Any) -> "CapabilityGapStatus | None":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).upper())
        except (ValueError, AttributeError):
            return None


def missing_key_for(requirements) -> str:
    """The normalized identity of a set of missing capabilities (spec §127).

    Order-independent and version-aware, so re-deriving the same gap from a
    redelivered event finds the existing row instead of opening a second one
    (spec §11).
    """
    names = sorted(
        f"{r.name}:{r.version_constraint}" if r.version_constraint else r.name
        for r in requirements
    )
    return ",".join(names)


@dataclass
class CapabilityGap:
    """A competence the system lacks, held apart from the need that revealed it.

    Attributes:
        work_requirement_id: The need this gap is blocking.
        required_capabilities: Everything that need asked for.
        missing_capabilities: The subset nothing currently provides — the gap
            proper.
        current_partial_providers: ``"name:version"`` of definitions that cover
            *some* of what was required.  Recorded because the interesting
            reuse question is usually "who is nearly right?".
        source_match_id: The :class:`~nexus_seed.capabilities.models.
            CapabilityWorkMatch` that concluded this, so the gap can be walked
            back to the matching attempt that produced it (spec §95).
    """

    work_requirement_id: uuid.UUID
    required_capabilities: list[CapabilityRequirement] = field(default_factory=list)
    missing_capabilities: list[CapabilityRequirement] = field(default_factory=list)
    current_partial_providers: list[str] = field(default_factory=list)
    reason: str | None = None
    source_match_id: uuid.UUID | None = None
    status: CapabilityGapStatus = CapabilityGapStatus.OPEN
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def missing_key(self) -> str:
        """Logical identity of this gap's missing set (spec §127)."""
        return missing_key_for(self.missing_capabilities)

    @property
    def missing_names(self) -> list[str]:
        """The missing capability names, in declaration order."""
        return [r.name for r in self.missing_capabilities]

    def summary(self) -> dict:
        """A compact description, for a prompt or an audit line (spec §53)."""
        return {
            "capability_gap_id": str(self.id),
            "work_requirement_id": str(self.work_requirement_id),
            "missing_capabilities": self.missing_names,
            "partial_providers": list(self.current_partial_providers),
            "reason": self.reason,
        }


# --- extension proposal -----------------------------------------------------


class ExtensionStrategy(str, Enum):
    """How a gap might be closed.

    Ordered from reuse to construction by :data:`~nexus_seed.extension.
    strategies.STRATEGY_ORDER`; the order is the point (Invariant 88).  Phase 5A
    only ever *proposes* one of these — naming ``ADD_PROCESS_DEFINITION`` writes
    no definition, and naming ``CODE_EXTENSION`` writes no code (spec §16).
    """

    REGISTER_EXISTING_PROCESS = "REGISTER_EXISTING_PROCESS"
    CONFIGURE_EXISTING_PROCESS = "CONFIGURE_EXISTING_PROCESS"
    CONNECT_EXISTING_BACKEND = "CONNECT_EXISTING_BACKEND"
    ADD_ADAPTER = "ADD_ADAPTER"
    ADD_EXTRACTOR = "ADD_EXTRACTOR"
    ADD_PROCESS_DEFINITION = "ADD_PROCESS_DEFINITION"
    ADD_EXTERNAL_PLUGIN = "ADD_EXTERNAL_PLUGIN"
    CODE_EXTENSION = "CODE_EXTENSION"
    #: No strategy this architecture can express (spec §65).  Never proposed —
    #: the gap is kept and said to be unserviceable, which is a truthful answer.
    UNSUPPORTED = "UNSUPPORTED"

    @classmethod
    def coerce(cls, value: Any) -> "ExtensionStrategy | None":
        """Return ``value`` as a strategy, or ``None`` if it is not one.

        ``None`` is load-bearing: an unknown strategy must reach validation as
        *unknown* rather than being mapped onto something plausible (spec §35).
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).upper())
        except (ValueError, AttributeError):
            return None


class ComponentType(str, Enum):
    """What kind of thing an extension would add."""

    PROCESS_DEFINITION = "PROCESS_DEFINITION"
    ACTION_BACKEND = "ACTION_BACKEND"
    INGRESS_ADAPTER = "INGRESS_ADAPTER"
    RESOURCE_EXTRACTOR = "RESOURCE_EXTRACTOR"
    PLUGIN = "PLUGIN"
    CODE_MODULE = "CODE_MODULE"
    CONFIGURATION = "CONFIGURATION"

    @classmethod
    def known(cls, value: Any) -> bool:
        """Whether ``value`` names a component type this system understands."""
        try:
            cls(str(value).upper())
        except (ValueError, AttributeError):
            return False
        return True


class ExtensionRisk(str, Enum):
    """How much of the system an extension would touch."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]

    @classmethod
    def coerce(cls, value: Any) -> "ExtensionRisk | None":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).upper())
        except (ValueError, AttributeError):
            return None

    @classmethod
    def highest(cls, *risks: "ExtensionRisk") -> "ExtensionRisk":
        """The most severe of ``risks`` (an unknown one is never the least)."""
        present = [r for r in risks if isinstance(r, cls)]
        if not present:
            return cls.CRITICAL
        return max(present, key=lambda r: r.rank)


_RISK_RANK = {
    ExtensionRisk.LOW: 0,
    ExtensionRisk.MEDIUM: 1,
    ExtensionRisk.HIGH: 2,
    ExtensionRisk.CRITICAL: 3,
}


class AcquisitionFeasibility(str, Enum):
    """Whether this architecture can express the strategy at all (spec §64)."""

    FEASIBLE = "FEASIBLE"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"

    @property
    def rank(self) -> int:
        return {"FEASIBLE": 0, "UNKNOWN": 1, "UNSUPPORTED": 2}[self.value]


class ExtensionProposalStatus(str, Enum):
    """Lifecycle of an :class:`ExtensionProposal`.

    APPROVED means one thing only (spec §22): this may be handed to a
    construction phase.  It is not a registration, an activation or a grant.
    """

    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INVALID = "INVALID"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"

    @property
    def live(self) -> bool:
        """Whether this proposal is still on its way to a decision."""
        return self in (
            ExtensionProposalStatus.PENDING,
            ExtensionProposalStatus.VALIDATED,
            ExtensionProposalStatus.REVIEW,
        )

    @classmethod
    def coerce(cls, value: Any) -> "ExtensionProposalStatus | None":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).upper())
        except (ValueError, AttributeError):
            return None


class ExtensionDecision(str, Enum):
    """What validation + policy concluded about an extension proposal."""

    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


#: Where a proposal's content came from.  Recorded rather than inferred: an
#: LLM-written description and a derived one must stay distinguishable.
SOURCE_DETERMINISTIC = "deterministic"
SOURCE_LLM = "llm"
SOURCE_HUMAN = "human"


@dataclass
class ProposedComponent:
    """One thing an extension would add, described rather than built.

    ``component_type`` is a plain string on purpose (spec §19): an unknown one
    must survive as far as validation, where it is refused by name, instead of
    being coerced into something known at parse time.
    """

    component_type: str
    name: str
    purpose: str = ""
    provides_capabilities: list[str] = field(default_factory=list)
    requires_capabilities: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "component_type": self.component_type,
            "name": self.name,
            "purpose": self.purpose,
            "provides_capabilities": list(self.provides_capabilities),
            "requires_capabilities": list(self.requires_capabilities),
            "required_permissions": list(self.required_permissions),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProposedComponent | None":
        """Build from untrusted input; ``None`` if it is not a component at all."""
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if not name:
            return None
        return cls(
            component_type=str(data.get("component_type", "")),
            name=str(name),
            purpose=str(data.get("purpose") or ""),
            provides_capabilities=_str_list(data.get("provides_capabilities")),
            requires_capabilities=_str_list(data.get("requires_capabilities")),
            required_permissions=_str_list(data.get("required_permissions")),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )

    @property
    def signature(self) -> str:
        """The part of this component that identifies it (for a fingerprint)."""
        provides = ",".join(sorted(self.provides_capabilities))
        return f"{self.component_type}:{self.name}[{provides}]"


@dataclass
class AcquisitionCandidate:
    """One way the gap could be closed, as the deterministic analyzer sees it.

    Candidates are produced before any model is asked anything (spec §25), and
    the set of strategies they name is the boundary a model's answer has to stay
    inside (spec §35).  A candidate is not a proposal: it records *that* a route
    exists and what it would involve, not how it would read to a human.
    """

    strategy: ExtensionStrategy
    target_capabilities: list[str] = field(default_factory=list)
    reusable_components: list[str] = field(default_factory=list)
    required_new_components: list[ProposedComponent] = field(default_factory=list)
    estimated_risk: ExtensionRisk = ExtensionRisk.MEDIUM
    estimated_cost: float | None = None
    feasibility: AcquisitionFeasibility = AcquisitionFeasibility.UNKNOWN
    required_permissions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        """A compact description — what a model is shown (spec §53)."""
        return {
            "strategy": self.strategy.value,
            "target_capabilities": list(self.target_capabilities),
            "reusable_components": list(self.reusable_components),
            "new_components": [c.name for c in self.required_new_components],
            "component_types": [c.component_type for c in self.required_new_components],
            "estimated_risk": self.estimated_risk.value,
            "estimated_cost": self.estimated_cost,
            "feasibility": self.feasibility.value,
            "required_permissions": list(self.required_permissions),
            "reasons": list(self.reasons),
        }

    def to_dict(self) -> dict:
        data = self.summary()
        data["required_new_components"] = [
            c.to_dict() for c in self.required_new_components
        ]
        return data


@dataclass
class ExtensionProposal:
    """A described extension that would close a gap — never an applied one.

    Attributes:
        strategy: The route this proposal takes, or ``None`` when what was
            offered was not a known strategy at all (spec §35).
        declared_strategy: What was literally offered, kept for the audit even
            when it could not be understood.
        target_capabilities: What acquiring this would provide.  Taken from the
            gap rather than invented (spec §74); a target outside the gap is a
            validation failure.
        proposed_components: What would have to be added.
        reusable_components: What already exists and would be used instead
            (Invariant 88).
        required_permissions: What *construction* would need.  Declaring a
            permission here grants nothing (spec §77, Invariant 90).
        candidate_strategies: The strategies the analyzer allowed, so the
            boundary a model had to stay inside stays visible afterwards.
        analysis: A snapshot of the candidates considered (spec §59).
        human_approval_required: Whether this may not be decided unattended.
        root_proposal_id / replaces_proposal_id: Modify-chain lineage
            (spec §47); a human edit produces a new proposal beside the old one.
    """

    capability_gap_id: uuid.UUID
    work_requirement_id: uuid.UUID | None = None
    target_capabilities: list[CapabilityRequirement] = field(default_factory=list)
    strategy: ExtensionStrategy | None = None
    declared_strategy: str | None = None
    title: str = ""
    description: str = ""
    proposed_components: list[ProposedComponent] = field(default_factory=list)
    reusable_components: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    candidate_strategies: list[str] = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    estimated_risk: ExtensionRisk = ExtensionRisk.CRITICAL
    estimated_cost: float | None = None
    feasibility: AcquisitionFeasibility = AcquisitionFeasibility.UNKNOWN
    human_approval_required: bool = True
    rationale: str | None = None
    source: str = SOURCE_DETERMINISTIC
    status: ExtensionProposalStatus = ExtensionProposalStatus.PENDING
    reasons: list[str] = field(default_factory=list)
    context_snapshot_id: uuid.UUID | None = None
    llm_invocation_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    root_proposal_id: uuid.UUID | None = None
    replaces_proposal_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.root_proposal_id is None:
            self.root_proposal_id = self.id
        if self.declared_strategy is None and self.strategy is not None:
            self.declared_strategy = self.strategy.value

    @property
    def target_names(self) -> list[str]:
        """The capability names this proposal says it would provide."""
        return [r.name for r in self.target_capabilities]

    @property
    def component_types(self) -> list[str]:
        return [c.component_type for c in self.proposed_components]

    @property
    def fingerprint(self) -> str:
        """The identity of this proposal's *content* (spec §68).

        Gap, strategy, targets and components — not the id, the wording or the
        time.  Two runs of the same analysis over the same registry produce the
        same fingerprint, which is what makes a redelivered
        ``capability_missing`` find the proposal already made instead of making
        a second one (spec §67).
        """
        strategy = self.declared_strategy or (
            self.strategy.value if self.strategy else "?"
        )
        targets = ",".join(sorted(self.target_names))
        components = ";".join(sorted(c.signature for c in self.proposed_components))
        reused = ",".join(sorted(self.reusable_components))
        payload = f"{self.capability_gap_id}|{strategy}|{targets}|{components}|{reused}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def summary(self) -> dict:
        """A compact description for events, traces and prompts."""
        return {
            "extension_proposal_id": str(self.id),
            "capability_gap_id": str(self.capability_gap_id),
            "strategy": self.declared_strategy,
            "title": self.title,
            "target_capabilities": self.target_names,
            "component_types": self.component_types,
            "required_permissions": list(self.required_permissions),
            "estimated_risk": self.estimated_risk.value,
            "feasibility": self.feasibility.value,
            "status": self.status.value,
        }

    @classmethod
    def from_output(
        cls,
        data: Any,
        *,
        capability_gap_id: uuid.UUID,
        work_requirement_id: uuid.UUID | None,
        target_capabilities: list[CapabilityRequirement],
        candidate_strategies: list[str],
        created_by_process_id: uuid.UUID | None = None,
        source: str = SOURCE_LLM,
        root_proposal_id: uuid.UUID | None = None,
        replaces_proposal_id: uuid.UUID | None = None,
    ) -> "ExtensionProposal | None":
        """Build a proposal from untrusted structured output.

        ``None`` means the output could not be read as a proposal at all — a
        schema failure the caller retries.  A *readable* output naming an
        unknown strategy or an unknown component type is deliberately **not**
        that: it is returned, so validation can refuse it by name and the
        attempt stays on the record (spec §35, §102).

        Targets are taken from the gap, never from the output (spec §74): a
        model may describe how to close this gap, not decide what the gap is.
        """
        if not isinstance(data, dict):
            return None
        declared = data.get("strategy")
        if declared is None:
            return None
        strategy = ExtensionStrategy.coerce(declared)

        components: list[ProposedComponent] = []
        raw_components = data.get("proposed_components")
        if isinstance(raw_components, list):
            for entry in raw_components:
                component = ProposedComponent.from_dict(entry)
                if component is not None:
                    components.append(component)

        risk = ExtensionRisk.coerce(data.get("estimated_risk"))
        proposal = cls(
            capability_gap_id=capability_gap_id,
            work_requirement_id=work_requirement_id,
            target_capabilities=list(target_capabilities),
            strategy=strategy,
            declared_strategy=str(declared),
            title=str(data.get("title") or "").strip(),
            description=str(data.get("description") or "").strip(),
            proposed_components=components,
            reusable_components=_str_list(data.get("reusable_components")),
            required_permissions=_str_list(data.get("required_permissions")),
            candidate_strategies=list(candidate_strategies),
            # An unreadable risk level is preserved as CRITICAL rather than
            # quietly downgraded: unparseable risk must never mean "safe".
            estimated_risk=risk if risk is not None else ExtensionRisk.CRITICAL,
            rationale=data.get("rationale"),
            source=source,
            created_by_process_id=created_by_process_id,
            root_proposal_id=root_proposal_id,
            replaces_proposal_id=replaces_proposal_id,
        )
        if strategy is None:
            proposal.reasons.append(f"strategy {declared!r} is not a known strategy")
        return proposal


@dataclass
class ExtensionDecisionRecord:
    """Why a proposal was approved, sent to review, or refused.

    Append-only (Invariant 92).  A proposal that was reviewed twice keeps both
    records, so "who allowed this, on what grounds, and when" survives every
    later change of mind.
    """

    extension_proposal_id: uuid.UUID
    decision: ExtensionDecision
    estimated_risk: ExtensionRisk = ExtensionRisk.CRITICAL
    capability_gap_id: uuid.UUID | None = None
    decided_by_process_id: uuid.UUID | None = None
    validation_ok: bool = True
    reasons: list[str] = field(default_factory=list)
    policy: dict = field(default_factory=dict)
    required_permissions: list[str] = field(default_factory=list)
    granted_permissions: list[str] = field(default_factory=list)
    reviewed_by_event_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


def _str_list(value: Any) -> list[str]:
    """Coerce untrusted input into a list of strings (never raising)."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


__all__ = [
    "SOURCE_DETERMINISTIC",
    "SOURCE_HUMAN",
    "SOURCE_LLM",
    "AcquisitionCandidate",
    "AcquisitionFeasibility",
    "CapabilityGap",
    "CapabilityGapStatus",
    "ComponentType",
    "ExtensionDecision",
    "ExtensionDecisionRecord",
    "ExtensionProposal",
    "ExtensionProposalStatus",
    "ExtensionRisk",
    "ExtensionStrategy",
    "ProposedComponent",
    "missing_key_for",
]
