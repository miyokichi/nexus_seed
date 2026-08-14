"""A choice is revalidated at the boundary where it would become executable."""

from __future__ import annotations

import pytest

from decision_helpers import (
    PlanStatus,
    SelectionProposalStatus,
    WorkStatus,
    candidates_ready,
    choice_work,
    compose_only,
    install_llm,
    offer_work,
    picks,
    plan_named,
    planning_runtime,
    register_alternatives,
    register_step,
    restore_selector,
    status_of,
)
from nexus_seed.decision.models import DecisionPreference


@pytest.mark.asyncio
async def test_llm_cannot_bypass_a_hard_constraint(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    register_step(
        runtime,
        "balanced_extract",
        "extract",
        ("raw",),
        ("m",),
        decision_metadata={
            "cost": 4.0,
            "latency": 8.0,
            "risk": 0.3,
            "quality": 0.8,
            "reliability": 0.9,
        },
    )
    requirement = choice_work(
        runtime,
        preference=DecisionPreference(max_risk=0.5),
    )
    compose_only(runtime)
    await offer_work(runtime, requirement)
    risky = plan_named(runtime, "fast_extract")
    assert len(runtime.get_plan_candidates(requirement.id)) == 3
    install_llm(runtime, picks(risky.id))
    restore_selector(runtime)

    await runtime.submit_event(candidates_ready(requirement))

    proposal = runtime.get_selection_proposals(requirement.id)[0]
    assert proposal.status is SelectionProposalStatus.INVALID
    assert status_of(runtime, requirement) is WorkStatus.BLOCKED_PLAN
    assert all(plan.status is PlanStatus.PROPOSED for plan in runtime.get_plans())
    runtime.close()


@pytest.mark.asyncio
async def test_disabled_definition_is_refused_at_final_validation(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    requirement = choice_work(runtime)
    compose_only(runtime)
    await offer_work(runtime, requirement)
    chosen = plan_named(runtime, "fast_extract")
    install_llm(runtime, picks(chosen.id))

    definition = runtime.process_store.get_definition("fast_extract", "1")
    definition.metadata["enabled"] = False
    runtime.process_store.upsert_definition(definition)
    restore_selector(runtime)

    await runtime.submit_event(candidates_ready(requirement))

    proposal = runtime.get_selection_proposals(requirement.id)[0]
    assert proposal.status is SelectionProposalStatus.INVALID
    assert runtime.get_plan(chosen.id).status is PlanStatus.PROPOSED
    assert all(
        node.process_instance_id is None
        for node in runtime.get_plan_nodes(chosen.id)
    )
    runtime.close()
