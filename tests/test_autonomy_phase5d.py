"""Phase 5D acceptance tests for bounded autonomous capability acquisition."""

from __future__ import annotations

from dataclasses import replace
import uuid

from extension_helpers import (
    POWERPOINT, block, elaborates, extension_runtime, gap_work,
    install_extension_llm, needs, register_disabled_provider,
)

from nexus_seed.autonomy.models import (
    AcquisitionStatus, AutonomyBudget, AutonomyDecisionKind,
    CapabilityAcquisitionSession,
)
from nexus_seed.backends.llm import proposal_response
from nexus_seed.backends.llm import FakeLLMBackend
from nexus_seed.construction.generator import LLMConstructionGenerator
from nexus_seed.extension.builder import LLMExtensionProposer
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.core.process import ProcessResult, ProcessStatus
from nexus_seed.core.effects import EffectConflictError, check_conflicts
import pytest
from nexus_seed.adapters.manual import ManualAdapter
from nexus_seed.runtime.drain import DrainBudget
from nexus_seed.processes.autonomy import AUTONOMY_REVIEWED, bootstrap_autonomy
from nexus_seed.work.work_requirement import WorkStatus
from nexus_seed.work.work_requirement import WorkRequirement
from capability_helpers import register_capable, worker


def autonomy_reviewed(session_id, decision="approve"):
    return Event(
        AUTONOMY_REVIEWED,
        "human",
        {"acquisition_session_id": str(session_id), "decision": decision},
    )


def test_autonomy_effect_conflicts_are_refused():
    session = CapabilityAcquisitionSession(
        capability_gap_id=uuid.uuid4(),
        source_work_requirement_id=uuid.uuid4(),
        acquisition_key="conflict",
    )
    other = replace(session, status=AcquisitionStatus.BLOCKED)
    result = ProcessResult(
        status=ProcessStatus.COMPLETED,
        acquisition_sessions=[session, other],
    )
    with pytest.raises(EffectConflictError, match="acquisition session"):
        check_conflicts(result)


async def test_auto_register_existing_process_closes_original_work(tmp_path):
    runtime = extension_runtime(tmp_path, "auto.db")
    bootstrap_autonomy(runtime)
    register_disabled_provider(runtime, "existing_reporter", "generate_report")
    requirement = gap_work(runtime, required=[needs("generate_report")])

    await block(runtime, requirement)

    session = runtime.get_acquisition_sessions()[0]
    assert session.status is AcquisitionStatus.COMPLETED
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    decisions = runtime.get_autonomy_decisions(session.id)
    assert [item.decision for item in decisions] == [
        AutonomyDecisionKind.AUTO,
        AutonomyDecisionKind.AUTO,
    ]
    assert runtime.get_installation_trace(session.installation_plan_id).activation is not None
    runtime.close()


async def test_new_process_waits_durably_then_human_approval_crosses_both_gates(tmp_path):
    path = "review.db"
    runtime = extension_runtime(tmp_path, path)
    bootstrap_autonomy(runtime)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    session = runtime.get_acquisition_sessions()[0]
    assert session.status is AcquisitionStatus.WAITING_REVIEW
    summary = session.review_summary
    assert summary["target_capabilities"][0]["name"] == "parse_powerpoint"
    runtime.close()

    runtime = extension_runtime(tmp_path, path)
    bootstrap_autonomy(runtime)
    await runtime.submit_event(autonomy_reviewed(session.id))
    session = runtime.get_acquisition_session(session.id)
    assert session.status is AcquisitionStatus.WAITING_REVIEW
    assert session.installation_plan_id is not None

    await runtime.submit_event(autonomy_reviewed(session.id))
    session = runtime.get_acquisition_session(session.id)
    assert session.status is AcquisitionStatus.COMPLETED
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    runtime.close()


async def test_forbidden_permission_blocks_and_has_no_human_override(tmp_path):
    runtime = extension_runtime(tmp_path, "forbidden.db")
    bootstrap_autonomy(runtime)
    install_extension_llm(runtime, proposal_response({
        "strategy": "CODE_EXTENSION",
        "title": "unsafe",
        "description": "attempt policy mutation",
        "proposed_components": [{
            "component_type": "CODE_MODULE", "name": "unsafe",
            "provides_capabilities": ["unsafe_capability"],
            "required_permissions": ["network.unrestricted"],
        }],
        "required_permissions": [
            "repository.modify", "filesystem.write", "network.unrestricted",
        ],
        "estimated_risk": "HIGH",
        "rationale": "test",
    }))
    requirement = gap_work(runtime, required=[needs("unsafe_capability")])
    await block(runtime, requirement)
    session = runtime.get_acquisition_sessions()[0]
    assert session.status is AcquisitionStatus.BLOCKED
    assert "AUTONOMY_FORBIDDEN" in session.blocked_reason

    await runtime.submit_event(autonomy_reviewed(session.id))
    assert runtime.get_acquisition_session(session.id).status is AcquisitionStatus.BLOCKED
    assert not runtime.get_construction_plans()
    runtime.close()


async def test_shared_gap_uses_one_session_for_two_work_requirements(tmp_path):
    runtime = extension_runtime(tmp_path, "shared.db")
    bootstrap_autonomy(runtime)
    first = gap_work(runtime, work_key="shared:1", required=[needs("parse_powerpoint", POWERPOINT)])
    second = gap_work(runtime, work_key="shared:2", required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, first)
    await block(runtime, second)

    sessions = runtime.get_acquisition_sessions()
    assert len(sessions) == 1
    assert len(runtime.autonomy_store.subscribers(sessions[0].id)) == 2
    await runtime.submit_event(autonomy_reviewed(sessions[0].id))
    assert runtime.get_acquisition_session(sessions[0].id).status is AcquisitionStatus.WAITING_REVIEW
    await runtime.submit_event(autonomy_reviewed(sessions[0].id))
    assert runtime.get_work_requirement(first.id).status is WorkStatus.SATISFIED
    assert runtime.get_work_requirement(second.id).status is WorkStatus.SATISFIED
    assert len(runtime.get_construction_plans()) == 1
    runtime.close()


async def test_depth_budget_and_cycle_are_hard_blocks(tmp_path):
    runtime = extension_runtime(tmp_path, "budget.db")
    bootstrap_autonomy(runtime)
    runtime.set_default_autonomy_budget(AutonomyBudget(max_extension_depth=0))
    requirement = WorkRequirement(
        work_type="demo_work", work_key="budget-depth", reason="test",
        required_capabilities=[needs("parse_powerpoint", POWERPOINT)],
        metadata={"extension_depth": 1},
    )
    runtime.work_requirement_store.save(requirement)
    await block(runtime, requirement)
    session = runtime.get_acquisition_sessions()[0]
    assert session.status is AcquisitionStatus.BLOCKED
    assert session.blocked_reason == "EXTENSION_DEPTH_EXHAUSTED"
    runtime.close()


def _generated_artifacts(implementation: str) -> dict:
    return {"artifacts": [
        {
            "relative_path": "extractors/parse_powerpoint.py",
            "content": implementation,
            "artifact_role": "implementation",
        },
        {
            "relative_path": "tests/test_parse_powerpoint.py",
            "content": "def test_shape():\n    assert True\n",
            "artifact_role": "test",
        },
        {
            "relative_path": "fixtures/parse_powerpoint.txt",
            "content": "marker\n",
            "artifact_role": "fixture",
        },
    ]}


async def test_construction_redesign_retries_once_then_converges(tmp_path):
    runtime = extension_runtime(tmp_path, "retry.db")
    bootstrap_autonomy(runtime)
    bad = proposal_response({"unexpected": True})
    good = proposal_response(_generated_artifacts(
        "def nexus_capability(fixture):\n    return {'verified': True}\n"
    ))
    runtime.set_llm_construction_generator(
        LLMConstructionGenerator(FakeLLMBackend(script=[bad, good]))
    )
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    session = runtime.get_acquisition_sessions()[0]
    await runtime.submit_event(autonomy_reviewed(session.id))

    session = runtime.get_acquisition_session(session.id)
    assert session.construction_attempts == 2
    assert len(runtime.get_construction_plans()) == 2
    assert session.status is AcquisitionStatus.WAITING_REVIEW
    await runtime.submit_event(autonomy_reviewed(session.id))
    assert runtime.get_acquisition_session(session.id).status is AcquisitionStatus.COMPLETED
    runtime.close()


async def test_construction_attempt_budget_exhaustion_blocks_without_cancelling_work(tmp_path):
    runtime = extension_runtime(tmp_path, "retry-limit.db")
    bootstrap_autonomy(runtime)
    runtime.set_default_autonomy_budget(AutonomyBudget(max_construction_attempts=2))
    runtime.set_llm_construction_generator(
        LLMConstructionGenerator(FakeLLMBackend(default=proposal_response({"unexpected": True})))
    )
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    session = runtime.get_acquisition_sessions()[0]
    await runtime.submit_event(autonomy_reviewed(session.id))

    session = runtime.get_acquisition_session(session.id)
    assert session.status is AcquisitionStatus.BLOCKED
    assert session.blocked_reason == "CONSTRUCTION_BUDGET_EXHAUSTED"
    assert len(runtime.get_construction_plans()) == 2
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.BLOCKED_CAPABILITY
    runtime.close()


async def test_cancelled_only_subscriber_closes_autonomy_review(tmp_path):
    runtime = extension_runtime(tmp_path, "cancel.db")
    bootstrap_autonomy(runtime)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    session = runtime.get_acquisition_sessions()[0]
    waiters = runtime.services.find_autonomy_review_continuations(session.id)
    assert len(waiters) == 1
    runtime.work_requirement_store.update_status(requirement.id, WorkStatus.CANCELLED)
    await runtime.submit_event(Event(
        "work_cancelled", "test", {"work_requirement_id": str(requirement.id)}
    ))
    assert runtime.get_acquisition_session(session.id).status is AcquisitionStatus.CANCELLED
    assert runtime.services.find_autonomy_review_continuations(session.id) == []
    runtime.close()


async def test_external_capability_race_stops_future_acquisition_stages(tmp_path):
    runtime = extension_runtime(tmp_path, "race.db")
    bootstrap_autonomy(runtime)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    session = runtime.get_acquisition_sessions()[0]

    register_capable(runtime, "provider_arrived", ("parse_powerpoint",))
    await runtime.run_pending()

    session = runtime.get_acquisition_session(session.id)
    assert session.status is AcquisitionStatus.CANCELLED
    assert session.blocked_reason == "CAPABILITY_ALREADY_AVAILABLE"
    assert runtime.get_construction_plans() == []
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    assert runtime.services.find_autonomy_review_continuations(session.id) == []
    runtime.close()


async def test_recursive_dependency_cycle_is_detected_and_stops_chain(tmp_path):
    runtime = extension_runtime(tmp_path, "cycle.db")
    bootstrap_autonomy(runtime)
    parent = proposal_response({
        "strategy": "ADD_EXTRACTOR", "title": "parent", "description": "parent",
        "proposed_components": [{
            "component_type": "RESOURCE_EXTRACTOR", "name": "parent_extractor",
            "provides_capabilities": ["parse_powerpoint"],
            "requires_capabilities": ["helper_format"],
            "required_permissions": [],
            "metadata": {"dependency_hints": {
                "helper_format": {"resource_type": "helperfmt", "representation": "structure"}
            }},
        }],
        "required_permissions": ["repository.read", "process.register"],
        "estimated_risk": "MEDIUM", "rationale": "test",
    })
    child = proposal_response({
        "strategy": "ADD_EXTRACTOR", "title": "child", "description": "child",
        "proposed_components": [{
            "component_type": "RESOURCE_EXTRACTOR", "name": "child_extractor",
            "provides_capabilities": ["helper_format"],
            "requires_capabilities": ["parse_powerpoint"],
            "required_permissions": [],
        }],
        "required_permissions": ["repository.read", "process.register"],
        "estimated_risk": "MEDIUM", "rationale": "test",
    })
    runtime.set_llm_extension_proposer(
        LLMExtensionProposer(FakeLLMBackend(script=[parent, child]))
    )
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    sessions = runtime.get_acquisition_sessions()
    assert len(sessions) == 2
    assert any(
        session.status is AcquisitionStatus.BLOCKED
        and session.blocked_reason == "ACQUISITION_CYCLE"
        for session in sessions
    )
    assert len(sessions) <= 2
    runtime.close()


async def test_recursive_dependency_acquires_child_before_parent_continues(tmp_path):
    runtime = extension_runtime(tmp_path, "recursive-success.db")
    bootstrap_autonomy(runtime)
    register_disabled_provider(runtime, "helper_provider", "helper_format")
    parent = proposal_response({
        "strategy": "ADD_EXTRACTOR", "title": "parent", "description": "parent",
        "proposed_components": [{
            "component_type": "RESOURCE_EXTRACTOR", "name": "parent_extractor",
            "provides_capabilities": ["parse_powerpoint"],
            "requires_capabilities": ["helper_format"],
            "required_permissions": [],
        }],
        "required_permissions": ["repository.read", "process.register"],
        "estimated_risk": "MEDIUM", "rationale": "test",
    })
    child = proposal_response({
        "strategy": "REGISTER_EXISTING_PROCESS", "title": "reuse helper",
        "description": "enable the exact helper provider",
        "proposed_components": [],
        "reusable_components": ["helper_provider:1"],
        "required_permissions": ["capability.enable"],
        "estimated_risk": "LOW", "rationale": "test",
    })
    runtime.set_llm_extension_proposer(
        LLMExtensionProposer(FakeLLMBackend(script=[parent, child]))
    )
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    sessions = runtime.get_acquisition_sessions()
    assert len(sessions) == 2
    parent_session = next(
        item for item in sessions if item.source_work_requirement_id == requirement.id
    )
    child_session = next(item for item in sessions if item.parent_session_id == parent_session.id)
    assert child_session.status is AcquisitionStatus.COMPLETED
    assert parent_session.status is AcquisitionStatus.WAITING_REVIEW

    await runtime.submit_event(autonomy_reviewed(parent_session.id))
    await runtime.submit_event(autonomy_reviewed(parent_session.id))
    assert runtime.get_acquisition_session(parent_session.id).status is AcquisitionStatus.COMPLETED
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    runtime.close()


async def _external_report_request(ctx):
    requirement = ctx.require_work(
        work_type="external_report", work_key="external:auto-report:1",
        reason="external occurrence requires a report",
        required_capabilities=[needs("generate_report")],
    )
    return ctx.complete(emitted_events=[ctx.new_event(
        "work_required", {"work_requirement_id": str(requirement.id)}
    )])


async def test_external_event_runs_the_complete_auto_acquisition_loop(tmp_path):
    runtime = extension_runtime(tmp_path, "external-auto.db")
    bootstrap_autonomy(runtime)
    register_disabled_provider(runtime, "existing_reporter", "generate_report")
    runtime.register_process(ProcessDefinition(
        name="external_report_request", version="1",
        handler="external_report_request",
        trigger_event_types=("external_report_request",),
    ), _external_report_request)
    envelope = ManualAdapter("phase5d-test").envelope(
        event_type="external_report_request", source_event_key="auto-report-001",
        payload={"report": "quarterly"},
    )

    receipt = await runtime.ingress.ingest(envelope)

    session = runtime.get_acquisition_sessions()[0]
    requirement = runtime.get_work_requirements()[0]
    assert receipt.event.ingress_receipt_id is not None
    assert session.status is AcquisitionStatus.COMPLETED
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    assert runtime.get_pending_event_delivery_count() == 0
    trace = runtime.get_acquisition_trace(session.id)
    assert trace.installation_trace.activation is not None
    assert len(trace.decisions) == 2
    runtime.close()


async def test_bounded_drain_restart_converges_without_duplicate_logical_records(tmp_path):
    name = "restart-auto.db"
    runtime = extension_runtime(tmp_path, name)
    bootstrap_autonomy(runtime)
    register_disabled_provider(runtime, "existing_reporter", "generate_report")
    requirement = gap_work(runtime, required=[needs("generate_report")])
    await runtime.submit_event(
        Event("work_required", "test", {"work_requirement_id": str(requirement.id)}),
        budget=DrainBudget(max_dispatches=2, max_activations=2, max_cycles=2),
    )
    runtime.close()

    runtime = extension_runtime(tmp_path, name)
    bootstrap_autonomy(runtime)
    register_disabled_provider(runtime, "existing_reporter", "generate_report")
    for _ in range(200):
        await runtime.run_pending(DrainBudget(max_dispatches=2, max_activations=2, max_cycles=2))
        sessions = runtime.get_acquisition_sessions()
        if (
            sessions
            and sessions[0].status is AcquisitionStatus.COMPLETED
            and runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
        ):
            break

    sessions = runtime.get_acquisition_sessions()
    assert len(sessions) == 1
    assert sessions[0].status is AcquisitionStatus.COMPLETED
    assert len(runtime.get_construction_plans()) == 1
    assert len(runtime.get_installation_plans()) == 1
    assert len(runtime.installation_store.active_activations()) == 1
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    runtime.close()
