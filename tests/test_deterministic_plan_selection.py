"""The non-LLM selector is stable and remains the system's execution floor."""

from __future__ import annotations

import uuid

from nexus_seed.decision.models import DecisionPreference, PlanEvaluation
from nexus_seed.decision.selector import DeterministicPlanSelector


def candidate(fingerprint: str, *, cost: float, rank: int = 0) -> PlanEvaluation:
    return PlanEvaluation(
        plan_id=uuid.uuid4(),
        fingerprint=fingerprint,
        estimated_cost=cost,
        estimated_risk=0.1,
        node_count=2,
        depth=2,
        metadata={"planner_rank": rank},
    )


def test_weighted_selection_is_stable_across_input_order() -> None:
    cheap = candidate("cheap", cost=1.0)
    expensive = candidate("expensive", cost=10.0)
    preference = DecisionPreference(weights={"cost": -1.0})
    selector = DeterministicPlanSelector()

    assert selector.select([cheap, expensive], preference).plan_id == cheap.plan_id
    assert selector.select([expensive, cheap], preference).plan_id == cheap.plan_id


def test_fingerprint_is_the_final_stable_tie_break() -> None:
    later = candidate("z-shape", cost=1.0)
    earlier = candidate("a-shape", cost=1.0)
    selector = DeterministicPlanSelector()

    assert selector.select([later, earlier], DecisionPreference()).plan_id == earlier.plan_id
