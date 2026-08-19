"""Phase 6 Persistent Being acceptance, safety and restart convergence tests."""

from __future__ import annotations

import uuid

import pytest

from extension_helpers import extension_runtime
from nexus_seed.autonomy.models import AcquisitionStatus, AutonomyDecisionKind
from nexus_seed.backends.action import FakeActionBackend
from nexus_seed.backends.llm import FakeLLMBackend, proposal_response
from nexus_seed.app import AppSettings
from nexus_seed.capabilities.models import CapabilityRef
from nexus_seed.context.requirements import ContextRequirements, ContinuationReq, WorkReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.presence import (
    ClaimStatus,
    IntentionStatus,
    get_experience_trace,
    get_intention,
    intention_id_for_pursuit,
    project_master,
    project_self,
)
from nexus_seed.processes.actions import (
    action_proposed_event,
    bootstrap_actions,
    waiting_for_action,
)
from nexus_seed.processes.autonomy import bootstrap_autonomy
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.processes.work_review import bootstrap_work_review
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


pytestmark = pytest.mark.asyncio


GOAL_WORKER = ProcessDefinition(
    name="phase6_goal_worker",
    version="1",
    handler="phase6_goal_worker",
    metadata={"role": "work", "permissions": ["filesystem.write"]},
    provides_capabilities=(CapabilityRef("advance_persistent_goal"),),
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        work=WorkReq(current=True),
        continuation=ContinuationReq(include=True),
    ),
)


async def phase6_goal_worker(ctx):
    """Perform self-initiated Work through the existing Action boundary."""

    if ctx.resume_point == "await_action":
        if ctx.event.type != "action_succeeded":
            ctx.mark_work(ctx.instance.work_requirement_id, WorkStatus.FAILED)
            return ctx.complete(
                output={"succeeded": False},
                emitted_events=[ctx.new_event("work_failed", {
                    "work_requirement_id": str(ctx.instance.work_requirement_id),
                    "work_key": ctx.instance.work_key,
                })],
            )
        ctx.satisfy_work()
        return ctx.complete(
            output={"succeeded": True},
            emitted_events=[ctx.new_event("work_satisfied", {
                "work_requirement_id": str(ctx.instance.work_requirement_id),
                "work_key": ctx.instance.work_key,
            })],
        )
    proposal = ctx.propose_action(
        backend="fake_action",
        action_type="write_file",
        target="phase6-result.txt",
        parameters={"content": "persistent intention fulfilled"},
        required_permissions=["filesystem.write"],
        declared_side_effects=["filesystem_write"],
        risk_level="LOW",
        rationale="fulfil the current persistent intention",
    )
    return ctx.suspend(
        resume_point="await_action",
        waiting_for=waiting_for_action(proposal),
        saved_process_state={"action_proposal_id": str(proposal.id)},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


async def concrete_goal_worker(ctx):
    """Concrete acquired capability that closes Work through the normal Event."""

    ctx.satisfy_work()
    return ctx.complete(
        output={"done": True},
        emitted_events=[ctx.new_event(
            "work_satisfied",
            {
                "work_requirement_id": str(ctx.instance.work_requirement_id),
                "work_key": ctx.instance.work_key,
            },
        )],
    )


async def phase5g_probe(ctx):
    """A pre-Phase-6 style Process used to prove failure isolation."""

    return ctx.complete(output={"phase5g_available": True})


async def test_phase6_off_is_a_strict_noop(tmp_path):
    runtime = Runtime(tmp_path / "off.db")
    try:
        bootstrap_work_review(runtime)
        definitions_before = runtime.process_store.all_definitions()
        events_before = runtime.event_store.all()
        assert bootstrap_persistent_being(runtime, enabled=False) is False
        assert runtime.process_store.all_definitions() == definitions_before
        assert runtime.event_store.all() == events_before
        assert not hasattr(runtime, "_phase6_bootstrapped")
    finally:
        runtime.close()


async def test_phase6_application_setting_defaults_on_and_allows_explicit_off(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("NEXUS_SEED_PHASE6_ENABLED", raising=False)
    assert AppSettings.from_env(tmp_path / "missing.env").phase6_enabled is True

    monkeypatch.setenv("NEXUS_SEED_PHASE6_ENABLED", "false")
    assert AppSettings.from_env(tmp_path / "missing.env").phase6_enabled is False











async def test_phase6_failure_does_not_disable_phase5g_runtime(tmp_path):
    runtime = Runtime(tmp_path / "failure-isolation.db")
    bootstrap_semantic(runtime)
    bootstrap_persistent_being(runtime, enabled=True, wake_on_start=False)
    runtime.register_process(
        ProcessDefinition(
            "phase5g_probe", "1", "phase5g_probe", trigger_event_types=("phase5g_probe",)
        ),
        phase5g_probe,
    )
    try:
        await runtime.submit_event(Event(
            "master_claim_observed", "test", {"master_id": "master-1"}
        ))
        failed = [
            instance
            for instance in runtime.process_store.all_instances()
            if instance.definition_name == "maintain_self_master_state"
        ]
        assert failed[-1].status is ProcessStatus.FAILED

        await runtime.submit_event(Event("phase5g_probe", "test", {}))
        probes = [
            instance
            for instance in runtime.process_store.all_instances()
            if instance.definition_name == "phase5g_probe"
        ]
        assert probes[-1].status is ProcessStatus.COMPLETED
        assert probes[-1].local_state["output"]["phase5g_available"] is True
    finally:
        runtime.close()


async def test_self_and_master_are_world_state_projections(tmp_path):
    runtime = Runtime(tmp_path / "projection.db")
    bootstrap_semantic(runtime)
    bootstrap_persistent_being(runtime, enabled=True, wake_on_start=False)
    runtime.register_process(
        ProcessDefinition(
            "capability_fixture", "1", "capability_fixture",
            provides_capabilities=(CapabilityRef("remember_safely"),),
        ),
        lambda ctx: ctx.complete(),
    )
    try:
        await runtime.submit_event(Event(
            "self_state_observed", "test", {"attribute": "identity", "value": "NEXUS SEED"}
        ))
        await runtime.submit_event(Event(
            "master_claim_inferred",
            "test",
            {
                "master_id": "master-1",
                "category": "preferences",
                "key": "language",
                "value": "Japanese",
                "confidence": 0.7,
            },
        ))
        self_view = project_self(runtime)
        master = project_master(runtime, "master-1")
        assert self_view.identity == "NEXUS SEED"
        assert self_view.available_capabilities == ("remember_safely",)
        assert master.preferences[0].status is ClaimStatus.INFERRED
        assert master.preferences[0].value == "Japanese"
        assert runtime.state_store.get_current("self", "available_capabilities") is None
    finally:
        runtime.close()





