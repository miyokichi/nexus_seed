"""InterpretationPolicy: confidence thresholds and consistency override."""

from __future__ import annotations

from nexus_seed.intelligence.policy import InterpretationPolicy
from nexus_seed.intelligence.proposal import ProposalDecision


def test_thresholds():
    policy = InterpretationPolicy(accept_threshold=0.85, review_threshold=0.60)
    assert policy.decide(0.95) is ProposalDecision.ACCEPT
    assert policy.decide(0.85) is ProposalDecision.ACCEPT
    assert policy.decide(0.70) is ProposalDecision.REVIEW
    assert policy.decide(0.60) is ProposalDecision.REVIEW
    assert policy.decide(0.40) is ProposalDecision.REJECT


def test_consistency_conflict_overrides_confidence():
    policy = InterpretationPolicy(accept_threshold=0.85, review_threshold=0.60)
    # Even a near-certain proposal cannot auto-accept against a state conflict.
    assert policy.decide(0.99, consistency_ok=False) is ProposalDecision.REVIEW
