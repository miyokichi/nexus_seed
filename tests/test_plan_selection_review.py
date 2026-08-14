"""Plan-level review is an ordinary restart-safe Continuation."""

from __future__ import annotations

import pytest

from decision_helpers import (
    DecisionPreference,
    PlanStatus,
    SelectionMethod,
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
    reviewed,
    selection_of,
    status_of,
)
from nexus_seed.core.process import ProcessStatus


async def awaiting_review(tmp_path):
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    requirement = choice_work(runtime)
    compose_only(runtime)
    await offer_work(runtime, requirement)
    careful = plan_named(runtime, "careful_extract")
    fast = plan_named(runtime, "fast_extract")
    install_llm(runtime, picks(careful.id, confidence=0.7))
    restore_selector(runtime)
    await runtime.submit_event(candidates_ready(requirement))
    proposal = runtime.get_selection_proposals(requirement.id)[0]
    return runtime, requirement, proposal, careful, fast


def selection_instances(runtime):
    return [
        instance
        for instance in runtime.process_store.all_instances()
        if instance.definition_name == "select_process_plan"
    ]


@pytest.mark.asyncio
async def test_medium_confidence_suspends_before_plan_execution(tmp_path) -> None:
    runtime, requirement, proposal, _careful, _fast = await awaiting_review(tmp_path)

    instances = selection_instances(runtime)
    assert proposal.status is SelectionProposalStatus.REVIEW
    assert len(instances) == 1
    assert instances[0].status is ProcessStatus.SUSPENDED
    assert runtime.continuation_store.for_instance(instances[0].id) is not None
    assert runtime.get_plan_selections(requirement.id) == []
    assert all(p.status is PlanStatus.PROPOSED for p in runtime.get_plans())
    runtime.close()


@pytest.mark.asyncio
async def test_human_approval_revalidates_and_selects(tmp_path) -> None:
    runtime, requirement, proposal, careful, _fast = await awaiting_review(tmp_path)

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    stored = runtime.get_selection_proposal(proposal.id)
    selection = selection_of(runtime, requirement)
    assert stored.status is SelectionProposalStatus.ACCEPTED
    assert selection.selection_method is SelectionMethod.HUMAN
    assert selection.selected_plan_id == careful.id
    assert status_of(runtime, requirement) is WorkStatus.SATISFIED
    assert runtime.continuation_store.all() == []
    runtime.close()


@pytest.mark.asyncio
async def test_human_rejection_runs_no_plan(tmp_path) -> None:
    runtime, requirement, proposal, _careful, _fast = await awaiting_review(tmp_path)

    await runtime.submit_event(reviewed(proposal.id, "reject"))

    assert runtime.get_selection_proposal(proposal.id).status is SelectionProposalStatus.REJECTED
    assert runtime.get_plan_selections(requirement.id) == []
    assert status_of(runtime, requirement) is WorkStatus.BLOCKED_PLAN
    assert all(p.status is PlanStatus.PROPOSED for p in runtime.get_plans())
    runtime.close()


@pytest.mark.asyncio
async def test_human_may_choose_another_valid_candidate(tmp_path) -> None:
    runtime, requirement, proposal, careful, fast = await awaiting_review(tmp_path)

    await runtime.submit_event(
        reviewed(proposal.id, "choose_alternative", selected_plan_id=fast.id)
    )

    selection = selection_of(runtime, requirement)
    assert selection.selection_method is SelectionMethod.HUMAN
    assert selection.selected_plan_id == fast.id
    assert runtime.get_plan(fast.id).status is PlanStatus.COMPLETED
    assert runtime.get_plan(careful.id).status is PlanStatus.SUPERSEDED
    runtime.close()


@pytest.mark.asyncio
async def test_explicit_approval_policy_reviews_deterministic_selection(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    requirement = choice_work(
        runtime,
        preference=DecisionPreference(require_human_approval=True),
    )

    await offer_work(runtime, requirement)

    proposal = runtime.get_selection_proposals(requirement.id)[0]
    instance = next(
        i for i in selection_instances(runtime) if i.status is ProcessStatus.SUSPENDED
    )
    assert proposal.status is SelectionProposalStatus.REVIEW
    assert proposal.llm_invocation_id is None
    assert runtime.continuation_store.for_instance(instance.id) is not None
    assert runtime.get_plan_selections(requirement.id) == []

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    assert selection_of(runtime, requirement).selection_method is SelectionMethod.HUMAN
    assert status_of(runtime, requirement) is WorkStatus.SATISFIED
    runtime.close()


@pytest.mark.asyncio
async def test_plan_metadata_can_require_review_without_an_llm(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_step(
        runtime,
        "sensitive_extract",
        "extract",
        ("raw",),
        ("m",),
        human_approval_required=True,
    )
    register_step(runtime, "analyze", "analyze", ("m",), ("report",))
    requirement = choice_work(runtime)

    await offer_work(runtime, requirement)

    proposal = runtime.get_selection_proposals(requirement.id)[0]
    assert proposal.status is SelectionProposalStatus.REVIEW
    assert runtime.get_plan_selections(requirement.id) == []
    assert all(
        node.process_instance_id is None
        for plan in runtime.get_plans()
        for node in runtime.get_plan_nodes(plan.id)
    )
    runtime.close()
