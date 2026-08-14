"""Restarting while a plan decision awaits review loses no logical state."""

from __future__ import annotations

import pytest

from decision_helpers import (
    SelectionMethod,
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
    restore_selector,
    reviewed,
    selection_of,
    status_of,
)


@pytest.mark.asyncio
async def test_review_continuation_survives_restart(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_alternatives(runtime)
    requirement = choice_work(runtime)
    compose_only(runtime)
    await offer_work(runtime, requirement)
    selected = plan_named(runtime, "careful_extract")
    install_llm(runtime, picks(selected.id, confidence=0.7))
    restore_selector(runtime)
    await runtime.submit_event(candidates_ready(requirement))
    proposal = runtime.get_selection_proposals(requirement.id)[0]
    runtime.close()

    restarted = planning_runtime(tmp_path)
    register_alternatives(restarted)
    await restarted.submit_event(reviewed(proposal.id, "approve"))

    selections = restarted.get_plan_selections(requirement.id)
    assert len(selections) == 1
    assert selections[0].selection_method is SelectionMethod.HUMAN
    assert selections[0].selected_plan_id == selected.id
    assert status_of(restarted, requirement) is WorkStatus.SATISFIED
    assert restarted.continuation_store.all() == []
    restarted.close()
