"""A finite replan policy blocks the arrangement, never cancels the need."""

from __future__ import annotations

import pytest

from decision_helpers import (
    WorkStatus,
    choice_work,
    offer_work,
    planning_runtime,
    register_step,
    status_of,
)
from planning_helpers import failing_worker, staged_worker


@pytest.mark.asyncio
async def test_max_replans_stops_after_the_configured_alternatives(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    for priority, name in enumerate(("first", "second", "third"), start=1):
        register_step(
            runtime,
            name,
            "extract",
            ("raw",),
            ("m",),
            priority=10 - priority,
            handler=failing_worker,
        )
    register_step(runtime, "analyze", "analyze", ("m",), ("report",), handler=staged_worker)
    requirement = choice_work(runtime, max_replans=2)

    await offer_work(runtime, requirement)

    attempts = runtime.get_replan_attempts(requirement.id)
    failed = [p for p in runtime.get_plans_for_work(requirement.id) if p.status.value == "FAILED"]
    stored = runtime.work_requirement_store.get(requirement.id)
    assert len(failed) == 3
    assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3]
    assert "replan limit reached" in attempts[-1].failure_reason
    assert stored.replan_count == 2
    assert stored.status is WorkStatus.BLOCKED_PLAN
    assert stored.status is not WorkStatus.CANCELLED
    assert len(runtime.event_store.by_type("replan_unavailable")) == 1
    runtime.close()
