"""Plan approval and Action approval remain independent safety boundaries."""

from __future__ import annotations

import pytest

from decision_helpers import choice_work, offer_work, planning_runtime, register_step
from nexus_seed.actions.models import ActionProposalStatus
from nexus_seed.backends.action import FakeActionBackend
from nexus_seed.processes.actions import (
    action_proposed_event,
    bootstrap_actions,
    waiting_for_action,
)


async def high_risk_publisher(ctx):
    if ctx.resume_point == "await_action":
        return ctx.complete(
            output={"outputs": [{"type": "report", "value": "published"}]}
        )
    proposal = ctx.propose_action(
        backend="fake_action",
        action_type="write_file",
        target="report.txt",
        parameters={"content": "report"},
        required_permissions=["filesystem.write"],
        declared_side_effects=["filesystem_write"],
        risk_level="HIGH",
        rationale="publish the selected plan's result",
    )
    return ctx.suspend(
        resume_point="await_action",
        waiting_for=waiting_for_action(proposal),
        saved_process_state={"action_proposal_id": str(proposal.id)},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


@pytest.mark.asyncio
async def test_selected_plan_does_not_auto_approve_high_risk_action(tmp_path) -> None:
    runtime = planning_runtime(tmp_path)
    bootstrap_actions(runtime)
    backend = FakeActionBackend()
    runtime.register_backend("fake_action", backend)
    register_step(runtime, "prepare", "prepare", ("raw",), ("m",), priority=10)
    publisher = register_step(
        runtime,
        "publish",
        "publish",
        ("m",),
        ("report",),
        priority=10,
        handler=high_risk_publisher,
    )
    publisher.metadata["permissions"] = ["filesystem.write"]
    runtime.process_store.upsert_definition(publisher)
    requirement = choice_work(
        runtime,
        required=("prepare", "publish"),
        inputs=("raw",),
        outputs=("report",),
    )

    await offer_work(runtime, requirement)

    assert len(runtime.get_plan_selections(requirement.id)) == 1
    proposals = runtime.get_action_proposals()
    assert len(proposals) == 1
    assert proposals[0].status is ActionProposalStatus.REVIEW
    assert backend.calls == []
    assert runtime.get_action_executions(proposals[0].id) == []
    runtime.close()
