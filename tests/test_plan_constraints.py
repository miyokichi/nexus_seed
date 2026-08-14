"""Phase 4C hard constraints are eligibility rules, not preferences."""

from __future__ import annotations

import uuid

from nexus_seed.decision.models import DecisionPreference, PlanEvaluation
from nexus_seed.decision.policy import check_constraints, eligible


def evaluation(**values) -> PlanEvaluation:
    return PlanEvaluation(plan_id=uuid.uuid4(), **values)


def test_hard_constraint_filters_a_risky_plan() -> None:
    safe = evaluation(estimated_risk=0.2)
    risky = evaluation(estimated_risk=0.8)

    allowed, rejected = eligible(
        [risky, safe], DecisionPreference(max_risk=0.4)
    )

    assert allowed == [safe]
    assert [item.plan_id for item in rejected] == [risky.plan_id]
    assert "exceeds" in rejected[0].violations[0]


def test_unknown_metric_does_not_masquerade_as_zero() -> None:
    unknown = evaluation(estimated_cost=None)

    check = check_constraints(unknown, DecisionPreference(max_cost=10.0))

    assert not check.ok
    assert "unknown" in check.violations[0]


def test_soft_weight_never_changes_constraint_eligibility() -> None:
    risky = evaluation(estimated_risk=0.8, estimated_quality=1.0)

    check = check_constraints(
        risky,
        DecisionPreference(max_risk=0.4, weights={"quality": 1_000_000.0}),
    )

    assert not check.ok
