"""Replanning compiles current Memory instead of reusing the first snapshot."""

from __future__ import annotations

import pytest

from decision_helpers import (
    LLMPlanSelector,
    WorkStatus,
    choice_work,
    planning_runtime,
    proposal_response,
    register_step,
    status_of,
)
from nexus_seed.runtime.drain import DrainBudget
from planning_helpers import failing_worker, staged_worker, work_required


class ChooseFirstBackend:
    """Select the first deterministic candidate and retain every request."""

    def __init__(self) -> None:
        self.calls = []

    async def execute(self, request):
        self.calls.append(request)
        selected = request.metadata["candidates"][0]["plan_id"]
        return proposal_response(
            {"selected_plan_id": selected, "confidence": 0.99, "rationale": "first"}
        )


@pytest.mark.asyncio
async def test_replan_selection_sees_current_related_state(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    register_step(
        runtime, "first", "extract", ("raw",), ("m",),
        priority=30, handler=failing_worker,
    )
    register_step(runtime, "second", "extract", ("raw",), ("m",), priority=20)
    register_step(runtime, "third", "extract", ("raw",), ("m",), priority=10)
    register_step(runtime, "analyze", "analyze", ("m",), ("report",))
    requirement = choice_work(runtime)
    requirement.related_entities = ["device"]
    runtime.db.execute("DELETE FROM work_requirements WHERE id = ?", (str(requirement.id),))
    runtime.work_requirement_store.save(requirement)
    backend = ChooseFirstBackend()
    runtime.set_llm_plan_selector(LLMPlanSelector(backend))
    budget = DrainBudget(max_activations=1)

    await runtime.submit_event(work_required(requirement), budget=budget)
    for _ in range(100):
        replan_events = runtime.event_store.by_type("replan_required")
        if replan_events:
            break
        await runtime.drain(budget)
    else:  # pragma: no cover - diagnostic guard
        raise AssertionError("the first plan never failed")

    assert len(backend.calls) == 1
    runtime.state_store.set("device", "mode", "current")

    for _ in range(100):
        await runtime.drain(budget)
        if status_of(runtime, requirement) is WorkStatus.SATISFIED:
            break
    else:  # pragma: no cover - diagnostic guard
        raise AssertionError("replanned work never completed")

    assert len(backend.calls) == 2
    current = backend.calls[1].context["world_state"]["device"]["mode"]
    assert current["value"] == "current"
    assert current["version"] == 1
    runtime.close()
