"""InterpretationPolicy — decide ACCEPT / REVIEW / REJECT from confidence.

Policy is domain configuration, never hardcoded in the Runtime (spec §16).  A
consistency failure forces at least REVIEW regardless of confidence (spec §15,
§48); schema failures are handled earlier (they never reach policy).
"""

from __future__ import annotations

from dataclasses import dataclass

from .proposal import ProposalDecision


@dataclass
class InterpretationPolicy:
    """Confidence thresholds for accepting an interpretation.

    ``confidence >= accept_threshold`` -> ACCEPT;
    ``review_threshold <= confidence < accept_threshold`` -> REVIEW;
    ``confidence < review_threshold`` -> REJECT.
    """

    accept_threshold: float = 0.85
    review_threshold: float = 0.60

    def decide(self, confidence: float, consistency_ok: bool = True) -> ProposalDecision:
        """Return the decision for a schema-valid proposal."""
        # A current-state conflict can never auto-accept; send to human review.
        if not consistency_ok:
            return ProposalDecision.REVIEW
        if confidence >= self.accept_threshold:
            return ProposalDecision.ACCEPT
        if confidence >= self.review_threshold:
            return ProposalDecision.REVIEW
        return ProposalDecision.REJECT
