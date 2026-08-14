"""Decision domain data — evaluating plans, choosing one, and recording why.

Phase 4B could compose a plan.  Phase 4C asks the question that only makes
sense once there is more than one: *which of these should we do, given what we
are trying to achieve and what we are not willing to risk?*

Nothing here is a core primitive (spec §3).  ``Decision``, ``Strategy``,
``Goal`` and ``Agent`` are deliberately absent: a decision is domain data
produced by an ordinary Process, exactly as an interpretation and an action
proposal already are.

The shape of the phase, in one line::

    candidates -> PlanEvaluation -> constraints -> selection -> PlanSelection

with an optional :class:`PlanSelectionProposal` in the middle when an LLM is
asked for an opinion.  The LLM's opinion is an input to that pipeline, never a
shortcut around it (Invariant 74).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.event import utcnow

#: What an unmeasured risk is treated as.
#:
#: Not zero (spec §12).  A process that never declared its risk has not
#: declared itself safe, and reading silence as safety is how a risk policy
#: quietly stops meaning anything.  Halfway is a deliberate compromise: high
#: enough that a strict ``max_risk`` rejects it, low enough that a permissive
#: one still runs.
UNKNOWN_RISK = 0.5

#: The metrics a preference may weigh or constrain.
METRICS = ("cost", "latency", "risk", "quality", "reliability")


class SelectionMethod(str, Enum):
    """How a plan came to be the chosen one (spec §37)."""

    DETERMINISTIC = "DETERMINISTIC"
    LLM = "LLM"
    HUMAN = "HUMAN"
    REPLAN = "REPLAN"


class SelectionProposalStatus(str, Enum):
    """Lifecycle of an LLM's *suggestion* about which plan to run (spec §26)."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REVIEW = "REVIEW"
    REJECTED = "REJECTED"
    #: The suggestion did not name a plan we could run — a hallucinated id, a
    #: plan outside the candidate set, or one that stopped being valid.
    INVALID = "INVALID"


@dataclass
class PlanEvaluation:
    """What one candidate plan is estimated to cost, take, risk and yield.

    Every field except ``estimated_risk`` may be ``None``, meaning **unknown**
    rather than zero (spec §12).  The distinction matters: a plan whose cost
    nobody declared is not a free plan, and summing it as zero would make the
    cheapest plan the one nobody bothered to describe.

    ``estimated_risk`` is always a number because risk is the one metric where
    silence must not be permissive — an unmeasured node contributes
    :data:`UNKNOWN_RISK`.
    """

    plan_id: uuid.UUID
    work_requirement_id: uuid.UUID | None = None
    fingerprint: str | None = None

    estimated_cost: float | None = None
    estimated_latency: float | None = None
    estimated_risk: float = UNKNOWN_RISK
    estimated_quality: float | None = None
    estimated_reliability: float | None = None

    node_count: int = 0
    depth: int = 0

    human_approval_required: bool = False

    reasons: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    def metric(self, name: str) -> float | None:
        """One metric by name, or ``None`` when it is unknown."""
        if name == "risk":
            return self.estimated_risk
        return getattr(self, f"estimated_{name}", None)

    @property
    def unknown_metrics(self) -> list[str]:
        """Which metrics this evaluation could not establish."""
        return [m for m in METRICS if self.metric(m) is None]

    @property
    def planner_rank(self) -> int:
        """Where the deterministic planner placed this candidate in its order.

        A tie-break of last resort that is still *meaningful*: the planner's
        order already accounts for capability priority and coverage, so
        preferring it beats preferring a hash when two plans score the same.
        """
        try:
            return int(self.metadata.get("planner_rank", 0))
        except (TypeError, ValueError):
            return 0

    def summary(self) -> dict:
        """A compact description, for an LLM prompt or an audit line (spec §61)."""
        return {
            "plan_id": str(self.plan_id),
            "fingerprint": self.fingerprint,
            "node_count": self.node_count,
            "depth": self.depth,
            "cost": self.estimated_cost,
            "latency": self.estimated_latency,
            "risk": self.estimated_risk,
            "quality": self.estimated_quality,
            "reliability": self.estimated_reliability,
            "approval_required": self.human_approval_required,
            "unknown": self.unknown_metrics,
        }

    def to_dict(self) -> dict:
        data = self.summary()
        data["reasons"] = list(self.reasons)
        data["metadata"] = dict(self.metadata)
        return data


@dataclass
class DecisionPreference:
    """What this work is trying to optimise, and what it will not accept.

    Two kinds of thing, kept apart on purpose (spec §20):

    * **hard constraints** (``max_*`` / ``min_*``) — a plan that violates one is
      not a worse option, it is not an option.  It never reaches a selector,
      and no confidence from any model can bring it back (Invariant 76).
    * **soft preferences** (``weights``) — how to rank the plans that remain.

    Weights are signed and read as "more of this is better": ``quality: 0.5``
    prefers higher quality, ``latency: -0.3`` prefers lower latency.
    """

    optimize_for: list[str] = field(default_factory=list)

    max_cost: float | None = None
    max_latency: float | None = None
    max_risk: float | None = None
    min_quality: float | None = None
    min_reliability: float | None = None

    weights: dict = field(default_factory=dict)

    #: Whether a metric a plan could not establish counts as a violation of a
    #: constraint on it.  True by default: "we cannot show this plan meets the
    #: limit" is not the same as "it does", and a hard constraint is exactly
    #: where that difference should be decided conservatively.
    unknown_violates_constraints: bool = True

    require_human_approval: bool = False

    @property
    def is_default(self) -> bool:
        """Whether this preference asks for anything at all."""
        return not (
            self.optimize_for
            or self.weights
            or self.require_human_approval
            or any(
                v is not None
                for v in (
                    self.max_cost,
                    self.max_latency,
                    self.max_risk,
                    self.min_quality,
                    self.min_reliability,
                )
            )
        )

    def to_dict(self) -> dict:
        return {
            "optimize_for": list(self.optimize_for),
            "max_cost": self.max_cost,
            "max_latency": self.max_latency,
            "max_risk": self.max_risk,
            "min_quality": self.min_quality,
            "min_reliability": self.min_reliability,
            "weights": dict(self.weights),
            "unknown_violates_constraints": self.unknown_violates_constraints,
            "require_human_approval": self.require_human_approval,
        }

    @classmethod
    def from_dict(cls, data) -> "DecisionPreference":
        if not isinstance(data, dict):
            return cls()
        weights = data.get("weights")
        return cls(
            optimize_for=list(data.get("optimize_for") or ()),
            max_cost=_as_float_or_none(data.get("max_cost")),
            max_latency=_as_float_or_none(data.get("max_latency")),
            max_risk=_as_float_or_none(data.get("max_risk")),
            min_quality=_as_float_or_none(data.get("min_quality")),
            min_reliability=_as_float_or_none(data.get("min_reliability")),
            weights=dict(weights) if isinstance(weights, dict) else {},
            unknown_violates_constraints=bool(
                data.get("unknown_violates_constraints", True)
            ),
            require_human_approval=bool(data.get("require_human_approval", False)),
        )


@dataclass
class ConstraintCheck:
    """Whether one plan is eligible at all, and why not if it is not."""

    plan_id: uuid.UUID
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass
class PlanSelectionProposal:
    """A suggestion about which candidate to run — not the decision.

    The same boundary Phases 3B and 3C drew (spec §140): the component that
    *proposes* is not the one that decides.  Here it is load-bearing in a new
    way, because the thing being proposed is which of several already-approved
    options to take — so the proposal can be wrong in exactly one interesting
    way, by naming something that is not on the list (Invariant 74).
    """

    work_requirement_id: uuid.UUID
    candidate_plan_ids: list[uuid.UUID] = field(default_factory=list)
    selected_plan_id: uuid.UUID | None = None
    confidence: float = 0.0
    rationale: str | None = None
    context_snapshot_id: uuid.UUID | None = None
    llm_invocation_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    status: SelectionProposalStatus = SelectionProposalStatus.PENDING
    reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def from_output(
        cls,
        data,
        *,
        work_requirement_id: uuid.UUID,
        candidate_plan_ids: list[uuid.UUID],
        created_by_process_id: uuid.UUID | None = None,
    ) -> "PlanSelectionProposal | None":
        """Build a proposal from backend structured output (``None`` if unusable).

        Only three fields are asked for (spec §27): which plan, how sure, and
        why.  A model is never asked to return a plan graph, so there is no
        graph to validate — and no path by which a malformed one could reach
        execution.
        """
        if not isinstance(data, dict):
            return None
        confidence = _as_confidence(data.get("confidence"))
        if confidence is None:
            # A malformed confidence is a schema failure.  It is different
            # from a well-formed answer naming a plan that does not exist:
            # callers retry the former and persist the latter as INVALID.
            return None
        raw_id = data.get("selected_plan_id")
        try:
            selected = uuid.UUID(str(raw_id)) if raw_id is not None else None
        except (ValueError, AttributeError, TypeError):
            # A non-uuid id is not a parse failure of the response; it is a
            # response naming a plan that cannot exist.  Keep it, and let
            # validation record it as INVALID rather than retrying forever.
            selected = None
        proposal = cls(
            work_requirement_id=work_requirement_id,
            candidate_plan_ids=list(candidate_plan_ids),
            selected_plan_id=selected,
            confidence=confidence,
            rationale=data.get("rationale"),
            created_by_process_id=created_by_process_id,
        )
        if selected is None and raw_id is not None:
            proposal.reasons.append(f"selected_plan_id {raw_id!r} is not a plan id")
        return proposal

    def to_dict(self) -> dict:
        return {
            "selected_plan_id": str(self.selected_plan_id) if self.selected_plan_id else None,
            "candidate_plan_ids": [str(i) for i in self.candidate_plan_ids],
            "confidence": self.confidence,
            "rationale": self.rationale,
            "reasons": list(self.reasons),
        }


@dataclass
class PlanSelection:
    """The decision itself: this plan, for this need, for these reasons.

    Kept separate from the proposal (spec §36) because they answer different
    questions.  A proposal records what was suggested; a selection records what
    the system committed to — including when nothing was suggested at all,
    which is the ordinary case.
    """

    work_requirement_id: uuid.UUID
    selected_plan_id: uuid.UUID | None = None
    selection_method: SelectionMethod = SelectionMethod.DETERMINISTIC
    deterministic_score: float | None = None
    selection_proposal_id: uuid.UUID | None = None
    replan_attempt: int = 0
    considered_plan_ids: list[uuid.UUID] = field(default_factory=list)
    rejected_plan_ids: list[uuid.UUID] = field(default_factory=list)
    decision_reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def succeeded(self) -> bool:
        """Whether a plan was actually chosen."""
        return self.selected_plan_id is not None


@dataclass
class ReplanAttempt:
    """One attempt to find another way after a plan failed terminally.

    Recorded as its own row so replanning is idempotent (spec §92): the logical
    identity of an attempt is *this need, after that plan failed*, so a
    redelivered ``replan_required`` finds the attempt already made rather than
    making a second one.
    """

    work_requirement_id: uuid.UUID
    previous_plan_id: uuid.UUID | None = None
    attempt_number: int = 1
    excluded_fingerprints: list[str] = field(default_factory=list)
    excluded_definitions: list[str] = field(default_factory=list)
    candidate_plan_ids: list[uuid.UUID] = field(default_factory=list)
    selected_plan_id: uuid.UUID | None = None
    failure_reason: str | None = None
    reasons: list[str] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def key(self) -> str:
        """Logical identity: this need, after that plan (spec §93)."""
        return f"{self.work_requirement_id}:{self.previous_plan_id}"


def _as_float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_confidence(value) -> float | None:
    """A confidence outside [0, 1] is a schema failure, not a low score."""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return confidence if 0.0 <= confidence <= 1.0 else None


__all__ = [
    "METRICS",
    "UNKNOWN_RISK",
    "ConstraintCheck",
    "DecisionPreference",
    "PlanEvaluation",
    "PlanSelection",
    "PlanSelectionProposal",
    "ReplanAttempt",
    "SelectionMethod",
    "SelectionProposalStatus",
]
