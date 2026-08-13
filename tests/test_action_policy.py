"""ActionPolicy — risk is configuration, not a Runtime rule (spec §18-§19)."""

from __future__ import annotations

from nexus_seed.actions.models import ActionDecision, RiskLevel
from nexus_seed.actions.policy import ActionPolicy


def test_default_policy_is_conservative():
    policy = ActionPolicy()
    assert policy.decide(RiskLevel.LOW) is ActionDecision.APPROVE
    assert policy.decide(RiskLevel.MEDIUM) is ActionDecision.APPROVE
    assert policy.decide(RiskLevel.HIGH) is ActionDecision.REVIEW
    assert policy.decide(RiskLevel.CRITICAL) is ActionDecision.REJECT


def test_policy_is_configurable_without_touching_code():
    strict = ActionPolicy(
        decisions={
            RiskLevel.LOW: ActionDecision.APPROVE,
            RiskLevel.MEDIUM: ActionDecision.REVIEW,
            RiskLevel.HIGH: ActionDecision.REJECT,
            RiskLevel.CRITICAL: ActionDecision.REJECT,
        }
    )
    assert strict.decide(RiskLevel.MEDIUM) is ActionDecision.REVIEW
    assert strict.decide(RiskLevel.HIGH) is ActionDecision.REJECT


def test_unmapped_risk_level_falls_back_to_review():
    """An unclassified action is never silently auto-approved."""
    policy = ActionPolicy(decisions={RiskLevel.LOW: ActionDecision.APPROVE})
    assert policy.decide(RiskLevel.CRITICAL) is ActionDecision.REVIEW


def test_policy_round_trips_through_definition_metadata():
    policy = ActionPolicy(
        decisions={
            RiskLevel.LOW: ActionDecision.APPROVE,
            RiskLevel.MEDIUM: ActionDecision.REVIEW,
            RiskLevel.HIGH: ActionDecision.REVIEW,
            RiskLevel.CRITICAL: ActionDecision.REJECT,
        }
    )
    restored = ActionPolicy.from_dict(policy.to_dict())
    assert restored.decisions == policy.decisions


def test_from_dict_tolerates_garbage_and_keeps_defaults():
    restored = ActionPolicy.from_dict({"NONSENSE": "APPROVE", "HIGH": "MAYBE"})
    assert restored.decide(RiskLevel.HIGH) is ActionDecision.REVIEW
    assert ActionPolicy.from_dict(None).decide(RiskLevel.LOW) is ActionDecision.APPROVE


def test_risk_level_coercion():
    assert RiskLevel.coerce("high") is RiskLevel.HIGH
    assert RiskLevel.coerce(RiskLevel.LOW) is RiskLevel.LOW
    assert RiskLevel.coerce("SEVERE") is None
    assert RiskLevel.coerce(None) is None
