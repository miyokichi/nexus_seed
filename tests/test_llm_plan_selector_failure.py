"""An unavailable or malformed selector is never a single point of failure."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from decision_helpers import (
    SelectionMethod,
    WorkStatus,
    candidates_ready,
    choice_work,
    compose_only,
    failure_response,
    install_llm,
    offer_work,
    register_alternatives,
    restore_selector,
    selection_of,
    status_of,
    wire,
)
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


@pytest.mark.asyncio
async def test_backend_failures_are_journalled_then_fall_back(tmp_path) -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = wire(Runtime(tmp_path / "failure.db", clock=clock))
    register_alternatives(runtime)
    requirement = choice_work(runtime)
    compose_only(runtime)
    await offer_work(runtime, requirement)
    backend = install_llm(runtime, failure_response("offline"))
    restore_selector(runtime)

    await runtime.submit_event(candidates_ready(requirement))
    clock.advance(2)
    await runtime.tick()
    clock.advance(4)
    await runtime.tick()

    selection = selection_of(runtime, requirement)
    selector_instance = next(
        i for i in runtime.process_store.all_instances()
        if i.definition_name == "select_process_plan"
    )
    invocations = runtime.get_llm_invocations(selector_instance.id)
    assert len(backend.calls) == 3
    assert len(invocations) == 3
    assert all(not invocation.success for invocation in invocations)
    assert selection.selection_method is SelectionMethod.DETERMINISTIC
    assert status_of(runtime, requirement) is WorkStatus.SATISFIED
    runtime.close()
