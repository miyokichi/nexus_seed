"""Selection, validation, fallback and provenance over persisted candidates."""

from __future__ import annotations

import uuid

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
    picks_nothing_real,
    plan_named,
    planning_runtime,
    register_alternatives,
    restore_selector,
    selection_of,
    status_of,
)


async def composed_choice(tmp_path, *, preference=None):
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    requirement = choice_work(runtime, preference=preference)
    compose_only(runtime)
    await offer_work(runtime, requirement)
    assert len(runtime.get_plan_candidates(requirement.id)) == 2
    return runtime, requirement


@pytest.mark.asyncio
async def test_hard_constraint_is_applied_before_selection(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    requirement = choice_work(
        runtime,
        preference=DecisionPreference(max_risk=0.4, weights={"cost": -1.0}),
    )

    await offer_work(runtime, requirement)

    careful = plan_named(runtime, "careful_extract")
    fast = plan_named(runtime, "fast_extract")
    selection = selection_of(runtime, requirement)
    assert selection.selected_plan_id == careful.id
    assert fast.id in selection.rejected_plan_ids
    assert runtime.get_plan(fast.id).status is PlanStatus.SUPERSEDED
    assert status_of(runtime, requirement) is WorkStatus.SATISFIED
    runtime.close()


@pytest.mark.asyncio
async def test_llm_may_select_only_a_valid_candidate(tmp_path) -> None:
    runtime, requirement = await composed_choice(tmp_path)
    careful = plan_named(runtime, "careful_extract")
    install_llm(runtime, picks(careful.id))
    restore_selector(runtime)

    await runtime.submit_event(candidates_ready(requirement))

    proposal = runtime.get_selection_proposals(requirement.id)[0]
    selection = selection_of(runtime, requirement)
    stored_work = runtime.work_requirement_store.get(requirement.id)
    assert proposal.status is SelectionProposalStatus.ACCEPTED
    assert selection.selection_method is SelectionMethod.LLM
    assert selection.selected_plan_id == careful.id
    assert stored_work.selected_plan_id == careful.id
    assert stored_work.status is WorkStatus.SATISFIED
    runtime.close()


@pytest.mark.asyncio
async def test_hallucinated_plan_is_invalid_and_runs_nothing(tmp_path) -> None:
    runtime, requirement = await composed_choice(tmp_path)
    install_llm(runtime, picks_nothing_real())
    restore_selector(runtime)

    await runtime.submit_event(candidates_ready(requirement))

    proposal = runtime.get_selection_proposals(requirement.id)[0]
    selection = selection_of(runtime, requirement)
    assert proposal.status is SelectionProposalStatus.INVALID
    assert selection.selected_plan_id is None
    assert status_of(runtime, requirement) is WorkStatus.BLOCKED_PLAN
    assert all(p.status is PlanStatus.PROPOSED for p in runtime.get_plans())
    assert all(n.process_instance_id is None for p in runtime.get_plans()
               for n in runtime.get_plan_nodes(p.id))
    runtime.close()


@pytest.mark.asyncio
async def test_low_confidence_falls_back_deterministically(tmp_path) -> None:
    preference = DecisionPreference(weights={"cost": -1.0})
    runtime, requirement = await composed_choice(tmp_path, preference=preference)
    careful = plan_named(runtime, "careful_extract")
    fast = plan_named(runtime, "fast_extract")
    install_llm(runtime, picks(careful.id, confidence=0.2))
    restore_selector(runtime)

    await runtime.submit_event(candidates_ready(requirement))

    proposal = runtime.get_selection_proposals(requirement.id)[0]
    selection = selection_of(runtime, requirement)
    assert proposal.status is SelectionProposalStatus.REJECTED
    assert selection.selection_method is SelectionMethod.DETERMINISTIC
    assert selection.selected_plan_id == fast.id
    assert status_of(runtime, requirement) is WorkStatus.SATISFIED
    runtime.close()


@pytest.mark.asyncio
async def test_decision_trace_reaches_the_llm_invocation(tmp_path) -> None:
    runtime, requirement = await composed_choice(tmp_path)
    careful = plan_named(runtime, "careful_extract")
    install_llm(runtime, picks(careful.id, rationale="safer"))
    restore_selector(runtime)

    await runtime.submit_event(candidates_ready(requirement))

    trace = runtime.get_decision_trace(requirement.id)
    assert len(trace.candidates) == 2
    assert len(trace.evaluations) == 2
    assert len(trace.proposals) == 1
    assert len(trace.llm_invocations) == 1
    assert trace.proposals[0].llm_invocation_id == trace.llm_invocations[0].id
    assert trace.selected_plan_id == careful.id
    runtime.close()
