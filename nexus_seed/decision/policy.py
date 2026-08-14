"""Hard constraints and the confidence policy for plan selection.

Two separate jobs that are easy to confuse and expensive to conflate
(spec §20):

* **constraints** decide *eligibility*.  A plan over ``max_risk`` is not a
  worse option, it is not an option, and nothing downstream — not a score, not
  a model's confidence, not a rationale — may put it back (Invariant 76).
* **policy** decides *who gets the final say* once the eligible set is known:
  the system, a human, or the deterministic selector as a fallback.

Both are domain configuration.  Neither is in the Runtime (spec §76).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import ConstraintCheck, DecisionPreference, PlanEvaluation

#: ``(attribute, metric, comparison)`` — the whole constraint vocabulary.
_LIMITS = (
    ("max_cost", "cost", "max"),
    ("max_latency", "latency", "max"),
    ("max_risk", "risk", "max"),
    ("min_quality", "quality", "min"),
    ("min_reliability", "reliability", "min"),
)


def check_constraints(
    evaluation: PlanEvaluation, preference: DecisionPreference | None
) -> ConstraintCheck:
    """Whether one plan is eligible under a preference's hard limits.

    An unknown metric counts as a violation by default
    (``unknown_violates_constraints``): "we cannot show this plan is under the
    limit" is not "it is", and a hard constraint is exactly the place to decide
    that conservatively rather than optimistically.
    """
    check = ConstraintCheck(plan_id=evaluation.plan_id)
    if preference is None:
        return check

    for attribute, metric, comparison in _LIMITS:
        limit = getattr(preference, attribute)
        if limit is None:
            continue
        value = evaluation.metric(metric)
        if value is None:
            if preference.unknown_violates_constraints:
                check.violations.append(
                    f"{metric} is unknown and {attribute}={limit} was required"
                )
            continue
        if comparison == "max" and value > limit:
            check.violations.append(f"{metric} {value} exceeds {attribute}={limit}")
        elif comparison == "min" and value < limit:
            check.violations.append(f"{metric} {value} is below {attribute}={limit}")
    return check


def eligible(
    evaluations: list[PlanEvaluation], preference: DecisionPreference | None
) -> tuple[list[PlanEvaluation], list[ConstraintCheck]]:
    """Split evaluations into those that may be selected and why the rest may not."""
    passed: list[PlanEvaluation] = []
    failures: list[ConstraintCheck] = []
    for evaluation in evaluations:
        check = check_constraints(evaluation, preference)
        if check.ok:
            passed.append(evaluation)
        else:
            failures.append(check)
    return passed, failures


class SelectionDecision(str, Enum):
    """What to do with an LLM's suggestion."""

    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    #: Too unsure to act on, but the need does not stop — hand back to the
    #: deterministic selector (spec §32).
    FALLBACK = "FALLBACK"
    REJECT = "REJECT"


@dataclass
class PlanSelectionPolicy:
    """Confidence thresholds for acting on a proposed selection (spec §31).

    ``low_confidence_falls_back`` is the Phase 4C default and the reason this
    policy differs from :class:`InterpretationPolicy`: an interpretation the
    system is unsure of can simply not be believed, but a *selection* it is
    unsure of still leaves a need that has to be met.  Refusing to choose would
    stall the work; choosing deterministically will not.
    """

    accept_threshold: float = 0.85
    review_threshold: float = 0.60
    low_confidence_falls_back: bool = True

    def decide(self, confidence: float, *, valid: bool = True) -> SelectionDecision:
        """Return the decision for a schema-valid, in-set proposal."""
        # A proposal that named something unusable is not a low-confidence
        # proposal; it is one there is nothing to act on.  In particular, a
        # hallucinated plan id must never cause *any* plan to run merely by
        # falling through to another selector (spec §83 / acceptance 7).
        if not valid:
            return SelectionDecision.REJECT
        if confidence >= self.accept_threshold:
            return SelectionDecision.ACCEPT
        if confidence >= self.review_threshold:
            return SelectionDecision.REVIEW
        return (
            SelectionDecision.FALLBACK
            if self.low_confidence_falls_back
            else SelectionDecision.REJECT
        )


__all__ = [
    "PlanSelectionPolicy",
    "SelectionDecision",
    "check_constraints",
    "eligible",
]
