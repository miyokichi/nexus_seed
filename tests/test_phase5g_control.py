"""Phase 5G human Command/Goal control-plane acceptance and restart tests."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends.base import BackendResult
from nexus_seed.backends.llm import FakeLLMBackend
from nexus_seed.capabilities.models import CapabilityRef
from nexus_seed.control.models import (
    CommandProposal,
    CommandStatus,
    GoalStatus,
    HumanIdentity,
)
from nexus_seed.control.parser import CommandParseError, parse_explicit_command
from nexus_seed.core.continuation import Continuation
from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessDefinition,
    ProcessInstance,
    ProcessStatus,
)
from nexus_seed.context.requirements import ContextRequirements, WorkReq, WorldStateReq
from nexus_seed.operations import submit_control_command
from nexus_seed.processes.control import bootstrap_control
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.providers.models import (
    ExecutionProvider,
    ProviderBinding,
    ProviderHealth,
    ProviderKind,
)
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


def authorize(runtime, identity="operator", permissions=("command.*",)):
    runtime.control_store.save_identity(HumanIdentity(identity, identity, permissions))


async def complete_human_work(ctx):
    ctx.satisfy_work()
    return ctx.complete(
        output={"done": True},
        emitted_events=[
            ctx.new_event(
                "work_satisfied",
                {"work_requirement_id": str(ctx.instance.work_requirement_id)},
            )
        ],
    )


async def complete_goal_work(ctx):
    ctx.satisfy_work()
    return ctx.complete(
        output={"goal_progress": True},
        emitted_events=[ctx.new_event(
            "work_satisfied", {"work_requirement_id": str(ctx.instance.work_requirement_id)}
        )],
    )


async def read_current_revision(ctx):
    return ctx.complete(output={"revision": ctx.view.get_state("project-a", "revision")})


async def finish_without_required_evidence(ctx):
    ctx.satisfy_work()
    return ctx.complete(output={"process_finished": True})


async def finish_with_required_evidence(ctx):
    ctx.satisfy_work({"summary_report_created": True})
    return ctx.complete(output={"process_finished": True})


def register_human_worker(runtime):
    definition = ProcessDefinition(
        "human_request_worker",
        "1",
        "complete_human_work",
        provides_capabilities=(CapabilityRef("fulfill_human_request"),),
    )
    runtime.register_process(definition, complete_human_work)
    return definition


def test_explicit_parser_and_natural_language_are_separate():
    command = parse_explicit_command(
        '/task create objective="Project A analysis" priority=HIGH cloud_forbidden=true',
        issuer_identity_id="u1",
        source_channel="discord",
        source_message_id="m1",
    )

    assert command.command_type == "task.create"
    assert command.arguments["objective"] == "Project A analysis"
    assert command.arguments["cloud_forbidden"] is True
    with pytest.raises(CommandParseError, match="start with"):
        parse_explicit_command(
            "Project Aの解析を優先して",
            issuer_identity_id="u1",
            source_channel="discord",
        )


async def test_explicit_work_command_reaches_existing_pipeline_and_is_idempotent(tmp_path):
    runtime = Runtime(tmp_path / "work-command.db")
    bootstrap_work_intelligence(runtime)
    register_human_worker(runtime)
    authorize(runtime)
    try:
        first = runtime.console.execute_text(
            '/task create objective="analyze Project A" priority=HIGH',
            issuer_identity_id="operator",
            source_message_id="slash-1",
        )
        duplicate = runtime.console.execute_text(
            '/task create objective="analyze Project A" priority=HIGH',
            issuer_identity_id="operator",
            source_message_id="slash-1",
        )
        await runtime.run_pending()

        work = runtime.get_work_requirements()[0]
        assert first.command_id == duplicate.command_id
        assert len(runtime.get_work_requirements()) == 1
        assert work.status is WorkStatus.SATISFIED
        assert work.human_priority == "HIGH"
        assert work.command_id == first.command_id
        assert runtime.control_store.get_command(first.command_id).status is CommandStatus.EXECUTED
    finally:
        runtime.close()


async def test_human_review_required_is_a_real_continuation_gate(tmp_path):
    runtime = Runtime(tmp_path / "work-review.db")
    bootstrap_control(runtime)
    bootstrap_work_intelligence(runtime)
    register_human_worker(runtime)
    authorize(runtime)
    try:
        created = runtime.console.execute_text(
            '/task create objective="review before execution" human_review_required=true',
            issuer_identity_id="operator",
            source_message_id="review-task",
        )
        await runtime.run_pending()
        work = runtime.get_work_requirement(uuid.UUID(created.data["id"]))
        assert work.status is WorkStatus.WAITING_REVIEW
        assert runtime.process_store.find_by_work_requirement_id(work.id) == []
        assert any(
            continuation.waiting_for == {
                "event_type": "work_reviewed",
                "work_requirement_id": str(work.id),
            }
            for continuation in runtime.continuation_store.all()
        )

        approved = runtime.console.execute_text(
            f"/approve {work.id}",
            issuer_identity_id="operator",
            source_message_id="approve-work",
        )
        assert approved.status is CommandStatus.EXECUTED
        await runtime.run_pending()
        assert runtime.get_work_requirement(work.id).status is WorkStatus.SATISFIED
    finally:
        runtime.close()


def test_unauthorized_high_impact_command_is_audited_and_changes_nothing(tmp_path):
    runtime = Runtime(tmp_path / "unauthorized.db")
    work = WorkRequirement("x", "x:1", status=WorkStatus.SPAWNED)
    runtime.work_requirement_store.save(work)
    authorize(runtime, "reader", ("command.work.read",))
    try:
        result = runtime.console.execute_text(
            f"/cancel {work.id}",
            issuer_identity_id="reader",
            source_message_id="m-cancel",
        )

        assert result.status is CommandStatus.REJECTED
        assert runtime.get_work_requirement(work.id).status is WorkStatus.SPAWNED
        stored = runtime.control_store.command_by_idempotency_key("cli:m-cancel")
        assert stored.status is CommandStatus.REJECTED
    finally:
        runtime.close()


def test_pause_and_resume_survive_restart_and_reactivate_fresh(tmp_path):
    database = tmp_path / "pause.db"
    runtime = Runtime(database)
    authorize(runtime)
    work = WorkRequirement("x", "x:pause", status=WorkStatus.SPAWNED)
    runtime.work_requirement_store.save(work)
    process = ProcessInstance(
        "worker", "1", status=ProcessStatus.RUNNABLE,
        work_requirement_id=work.id, work_key=work.work_key,
    )
    runtime.process_store.save_instance(process)
    paused = runtime.console.execute_text(
        f"/pause {work.id}", issuer_identity_id="operator", source_message_id="pause-1"
    )
    assert paused.status is CommandStatus.EXECUTED
    runtime.close()

    restarted = Runtime(database)
    try:
        assert restarted.get_work_requirement(work.id).status is WorkStatus.PAUSED
        assert restarted.process_store.get_instance(process.id).status is ProcessStatus.PAUSED
        resumed = restarted.console.execute_text(
            f"/resume {work.id}", issuer_identity_id="operator", source_message_id="resume-1"
        )
        assert resumed.status is CommandStatus.EXECUTED
        assert restarted.get_work_requirement(work.id).status is WorkStatus.SPAWNED
        assert restarted.process_store.get_instance(process.id).status is ProcessStatus.RUNNABLE
        assert "fresh" in resumed.message
    finally:
        restarted.close()


async def test_resume_activation_compiles_current_state_not_old_context(tmp_path):
    runtime = Runtime(tmp_path / "fresh-resume.db")
    authorize(runtime)
    definition = ProcessDefinition(
        "read_revision",
        "1",
        "read_current_revision",
        context_requirements=ContextRequirements(
            world_state=WorldStateReq(include_work_entities=True),
            work=WorkReq(current=True),
        ),
    )
    runtime.register_process(definition, read_current_revision)
    work = WorkRequirement(
        "read_revision", "revision:1", related_entities=["project-a"],
        status=WorkStatus.SPAWNED,
    )
    runtime.work_requirement_store.save(work)
    runtime.state_store.set("project-a", "revision", 1)
    process = ProcessInstance(
        definition.name, definition.version, work_requirement_id=work.id, work_key=work.work_key
    )
    runtime.process_store.save_instance(process)
    try:
        runtime.console.execute_text(
            f"/pause {work.id}", issuer_identity_id="operator", source_message_id="fresh-pause"
        )
        runtime.state_store.set("project-a", "revision", 2)
        runtime.console.execute_text(
            f"/resume {work.id}", issuer_identity_id="operator", source_message_id="fresh-resume"
        )
        await runtime.run_pending()
        stored = runtime.process_store.get_instance(process.id)
        assert stored.local_state["output"]["revision"] == 2
    finally:
        runtime.close()


class MustNotRunCloudAdapter:
    async def execute(self, request):  # pragma: no cover - failure is the assertion
        raise AssertionError("cloud provider must be removed by the hard constraint")


async def test_cloud_forbidden_removes_cloud_provider_and_local_executes(tmp_path):
    runtime = Runtime(tmp_path / "provider-constraint.db")
    bootstrap_work_intelligence(runtime)
    definition = register_human_worker(runtime)
    cloud = ExecutionProvider(
        "cloud-agent", "1", ProviderKind.EXTERNAL_AGENT, "cloud-test",
        health=ProviderHealth.HEALTHY, priority=1000, metadata={"cloud": True},
    )
    runtime.register_provider(cloud, MustNotRunCloudAdapter())
    runtime.register_provider_binding(
        ProviderBinding(definition.name, definition.version, cloud.id, priority=1000)
    )
    authorize(runtime)
    try:
        runtime.console.execute_text(
            '/task create objective="local only" cloud_forbidden=true',
            issuer_identity_id="operator",
            source_message_id="local-only",
        )
        await runtime.run_pending()
        work = runtime.get_work_requirements()[0]
        process = runtime.process_store.find_by_work_requirement_id(work.id)[0]
        selected = runtime.provider_store.selections_for_instance(process.id)[0]

        assert work.status is WorkStatus.SATISFIED
        assert selected.provider_id != cloud.id
        assert any("constraints narrowed" in reason for reason in selected.reasons)
    finally:
        runtime.close()


async def test_required_unavailable_provider_blocks_without_fallback(tmp_path):
    runtime = Runtime(tmp_path / "provider-required.db")
    definition = register_human_worker(runtime)
    work = WorkRequirement(
        "x", "x:required", status=WorkStatus.SPAWNED,
        required_capabilities=[],
        provider_directive={"kind": "REQUIRE", "provider": "missing-agent"},
    )
    runtime.work_requirement_store.save(work)
    process = ProcessInstance(
        definition.name, definition.version, work_requirement_id=work.id, work_key=work.work_key
    )
    runtime.process_store.save_instance(process)
    try:
        await runtime.run_pending()
        assert runtime.get_work_requirement(work.id).status is WorkStatus.BLOCKED_PROVIDER
        assert runtime.process_store.get_instance(process.id).status is ProcessStatus.COMPLETED
    finally:
        runtime.close()


async def test_process_completion_does_not_bypass_completion_criteria(tmp_path):
    runtime = Runtime(tmp_path / "criteria.db")
    runtime.register_process(
        ProcessDefinition("without_evidence", "1", "finish_without_required_evidence"),
        finish_without_required_evidence,
    )
    runtime.register_process(
        ProcessDefinition("with_evidence", "1", "finish_with_required_evidence"),
        finish_with_required_evidence,
    )
    first = WorkRequirement(
        "report", "report:no-evidence", status=WorkStatus.SPAWNED,
        completion_criteria=["summary_report_created"],
    )
    second = WorkRequirement(
        "report", "report:evidence", status=WorkStatus.SPAWNED,
        completion_criteria=["summary_report_created"],
    )
    runtime.work_requirement_store.save(first)
    runtime.work_requirement_store.save(second)
    runtime.process_store.save_instance(
        ProcessInstance("without_evidence", "1", work_requirement_id=first.id, work_key=first.work_key)
    )
    runtime.process_store.save_instance(
        ProcessInstance("with_evidence", "1", work_requirement_id=second.id, work_key=second.work_key)
    )
    try:
        await runtime.run_pending()
        assert runtime.get_work_requirement(first.id).status is WorkStatus.SPAWNED
        assert runtime.get_work_requirement(second.id).status is WorkStatus.SATISFIED
    finally:
        runtime.close()


async def test_goal_evaluation_restart_and_work_dedup_converge(tmp_path):
    database = tmp_path / "goal.db"
    criterion = json.dumps(
        [
            {
                "type": "required_work",
                "semantic_key": "analysis",
                "objective": "analyze Project A",
                "required_capabilities": ["analyze_project"],
            }
        ],
        separators=(",", ":"),
    )
    runtime = Runtime(database)
    bootstrap_control(runtime)
    authorize(runtime)
    created = runtime.console.execute_text(
        f"/goal create objective=review-ready success='{criterion}'",
        issuer_identity_id="operator",
        source_message_id="goal-1",
    )
    await runtime.run_pending()
    goal_id = uuid.UUID(created.data["id"])
    assert len(runtime.work_requirement_store.for_goal(goal_id)) == 1
    runtime.close()

    restarted = Runtime(database)
    bootstrap_control(restarted)
    try:
        await restarted.submit_event(
            Event("goal_evaluation_requested", "test", {"goal_id": str(goal_id)})
        )
        await restarted.submit_event(
            Event("goal_evaluation_requested", "test", {"goal_id": str(goal_id)})
        )
        works = restarted.work_requirement_store.for_goal(goal_id)
        assert len(works) == 1

        restarted.work_requirement_store.update_status(works[0].id, WorkStatus.SATISFIED)
        await restarted.submit_event(
            Event("work_satisfied", "test", {"work_requirement_id": str(works[0].id)})
        )
        assert restarted.control_store.get_goal(goal_id).status is GoalStatus.ACHIEVED
    finally:
        restarted.close()


async def test_full_goal_e2e_generates_executes_and_achieves(tmp_path):
    runtime = Runtime(tmp_path / "goal-e2e.db")
    bootstrap_control(runtime)
    bootstrap_work_intelligence(runtime)
    runtime.register_process(
        ProcessDefinition(
            "goal_worker",
            "1",
            "complete_goal_work",
            provides_capabilities=(CapabilityRef("advance_human_goal"),),
        ),
        complete_goal_work,
    )
    authorize(runtime)
    try:
        created = runtime.console.execute_text(
            '/goal create title="Project A readiness" objective="make Project A review ready" priority=HIGH',
            issuer_identity_id="operator",
            source_message_id="goal-e2e",
        )
        await runtime.run_pending()
        goal_id = uuid.UUID(created.data["id"])
        goal = runtime.control_store.get_goal(goal_id)
        works = runtime.work_requirement_store.for_goal(goal_id)

        assert goal.status is GoalStatus.ACHIEVED
        assert len(works) == 1
        assert works[0].status is WorkStatus.SATISFIED
    finally:
        runtime.close()


def test_ambiguous_natural_language_proposal_never_selects_a_target(tmp_path):
    runtime = Runtime(tmp_path / "proposal.db")
    authorize(runtime)
    try:
        proposal = CommandProposal(
            "work.cancel", "work", None, {}, 0.8, "stop that analysis",
            candidate_target_ids=(str(uuid.uuid4()), str(uuid.uuid4())),
        )
        result = runtime.console.execute_proposal(
            proposal, issuer_identity_id="operator"
        )
        assert result.status is CommandStatus.NEEDS_CLARIFICATION
        assert len(result.data["candidate_target_ids"]) == 2
    finally:
        runtime.close()


def test_high_impact_proposal_waits_for_confirmation_then_revalidates(tmp_path):
    runtime = Runtime(tmp_path / "proposal-confirm.db")
    authorize(runtime)
    work = WorkRequirement("x", "proposal:cancel", status=WorkStatus.SPAWNED)
    runtime.work_requirement_store.save(work)
    proposal = CommandProposal(
        "work.cancel", "work", str(work.id), {}, 0.99, "explicit target inferred"
    )
    try:
        waiting = runtime.console.execute_proposal(proposal, issuer_identity_id="operator")
        assert waiting.status is CommandStatus.WAITING_CONFIRMATION
        assert runtime.get_work_requirement(work.id).status is WorkStatus.SPAWNED

        confirmed = runtime.console.execute_proposal(
            proposal, issuer_identity_id="operator", confirmed=True
        )
        assert confirmed.status is CommandStatus.EXECUTED
        assert runtime.get_work_requirement(work.id).status is WorkStatus.CANCELLED
    finally:
        runtime.close()


async def test_llm_only_builds_proposal_then_normal_validation_executes(tmp_path):
    runtime = Runtime(tmp_path / "natural-proposal.db")
    authorize(runtime)
    work = WorkRequirement("x", "proposal:priority", status=WorkStatus.EXPECTED)
    runtime.work_requirement_store.save(work)
    runtime.register_backend(
        "llm",
        FakeLLMBackend(default=BackendResult(parsed_output={
            "command_type": "work.priority",
            "target_type": "work",
            "target_id": str(work.id),
            "arguments": {"priority": "HIGH"},
            "confidence": 0.93,
            "explanation": "the human explicitly named this work",
        })),
    )
    try:
        proposal = await runtime.console.propose_natural_language(
            "この解析を優先して",
            candidate_targets=[{"id": str(work.id), "type": "work"}],
        )
        assert runtime.get_work_requirement(work.id).priority == 0

        result = runtime.console.execute_proposal(proposal, issuer_identity_id="operator")
        assert result.status is CommandStatus.EXECUTED
        assert runtime.get_work_requirement(work.id).human_priority == "HIGH"
    finally:
        runtime.close()


def test_control_review_reuses_existing_review_event(tmp_path):
    runtime = Runtime(tmp_path / "review.db")
    authorize(runtime)
    proposal_id = str(uuid.uuid4())
    process = ProcessInstance("interpret_event_llm", "1", status=ProcessStatus.SUSPENDED)
    runtime.process_store.save_instance(process)
    runtime.continuation_store.save(
        Continuation(
            process.id,
            "await_review",
            {"event_type": "interpretation_reviewed", "proposal_id": proposal_id},
        )
    )
    try:
        result = runtime.console.execute_text(
            f"/approve {proposal_id}",
            issuer_identity_id="operator",
            source_message_id="approve-1",
        )
        event = runtime.event_store.get(uuid.UUID(result.emitted_events[0]))
        assert event.type == "interpretation_reviewed"
        assert event.payload["decision"] == "approve"
    finally:
        runtime.close()


async def test_http_control_endpoint_uses_same_console_service(tmp_path):
    runtime = Runtime(tmp_path / "http-control.db")
    authorize(runtime)
    ingress = WebhookIngress(runtime.ingress, token="secret")
    server = await WebhookServer(
        ingress,
        port=0,
        console=runtime.console,
        control_identity_id="operator",
    ).start()
    try:
        result = await asyncio.to_thread(
            submit_control_command,
            host="127.0.0.1",
            port=server.bound_port,
            token="secret",
            command="/status",
            idempotency_key="http-status-1",
        )
        assert result["status"] == "EXECUTED"
        assert "outstanding_event_deliveries" in result["data"]
    finally:
        await server.stop()
        runtime.close()
