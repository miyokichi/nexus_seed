"""Phase 5B acceptance: build and prove in sandbox, never activate."""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_disabled_provider,
    reviewed,
)

from nexus_seed.construction.models import (
    ConstructionPlanStatus,
    ConstructionResultStatus,
    SandboxWorkspaceStatus,
)
from nexus_seed.construction.validator import validate_relative_path
from nexus_seed.construction.generator import LLMConstructionGenerator
from nexus_seed.backends.llm import FakeLLMBackend, proposal_response
from nexus_seed.backends.action import ActionRequest
from nexus_seed.actions.models import ActionProposal, ActionProposalStatus, RiskLevel
from nexus_seed.core.event import Event
from nexus_seed.processes.construction import bootstrap_construction
from nexus_seed.runtime.drain import DrainBudget
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.processes.extension import bootstrap_extension
from nexus_seed.work.work_requirement import WorkStatus


async def _approved_runtime(tmp_path, *, reuse=False):
    runtime = extension_runtime(tmp_path)
    bootstrap_construction(runtime)
    if reuse:
        register_disabled_provider(runtime, "existing_reporter", "generate_report")
        requirement = gap_work(runtime, required=[needs("generate_report")])
    else:
        requirement = gap_work(
            runtime, required=[needs("parse_powerpoint", POWERPOINT)]
        )
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    await runtime.submit_event(reviewed(proposal.id, "approve"))
    return runtime, requirement, proposal


def test_construction_paths_are_relative_and_confined():
    assert validate_relative_path("extractors/powerpoint.py") is None
    for path in ("../escape.py", "/absolute.py", r"C:\production.py", r"..\escape.py"):
        assert validate_relative_path(path) is not None


async def test_add_extractor_reaches_verified_without_activation(tmp_path):
    runtime, requirement, proposal = await _approved_runtime(tmp_path)

    plans = runtime.get_construction_plans(proposal.id)
    assert len(plans) == 1
    plan = plans[0]
    result = runtime.get_construction_result(plan.id)
    workspace = runtime.construction_store.get_workspace_for_plan(plan.id)

    assert plan.status is ConstructionPlanStatus.VERIFIED
    assert result.status is ConstructionResultStatus.VERIFIED
    assert result.provided_capabilities == ["parse_powerpoint"]
    assert workspace.status is SandboxWorkspaceStatus.SEALED
    assert runtime.get_capability("parse_powerpoint") is None
    assert runtime.get_work_requirement(requirement.id).status.value == "BLOCKED_CAPABILITY"
    assert only_gap(runtime).status.value == "PROPOSAL_APPROVED"
    assert all(a.parameters["construction_plan_id"] == str(plan.id) for a in runtime.get_action_proposals() if a.backend == "construction_sandbox")
    runtime.close()


async def test_generated_artifacts_are_resources_with_full_trace(tmp_path):
    runtime, _requirement, proposal = await _approved_runtime(tmp_path)
    plan = runtime.get_construction_plans(proposal.id)[0]
    trace = runtime.get_construction_trace(plan.id)

    assert trace is not None
    assert trace.production_activated is False
    assert len(trace.generated_resources) == len(plan.expected_artifacts)
    assert len(trace.resource_versions) == len(plan.expected_artifacts)
    assert len(trace.representations) == len(plan.expected_artifacts)
    assert len(trace.actions) == len(plan.expected_artifacts)
    assert {c.layer.value for c in trace.verification_checks} == {
        "STRUCTURAL", "STATIC", "BEHAVIOR"
    }
    for resource in trace.generated_resources:
        assert resource.metadata["construction_plan_id"] == str(plan.id)
        assert resource.metadata["extension_proposal_id"] == str(proposal.id)
    runtime.close()


async def test_register_existing_process_is_verified_but_stays_disabled(tmp_path):
    runtime, _requirement, proposal = await _approved_runtime(tmp_path, reuse=True)
    plan = runtime.get_construction_plans(proposal.id)[0]

    assert runtime.get_construction_result(plan.id).status is ConstructionResultStatus.VERIFIED
    assert not runtime.get_capability("generate_report", "1").enabled
    assert list(runtime.workspace_manager.root_for(runtime.construction_store.get_workspace_for_plan(plan.id).id).glob("manifests/*.json"))
    runtime.close()


def _llm_artifacts(implementation: str) -> dict:
    return {
        "artifacts": [
            {"relative_path": "extractors/parse_powerpoint.py", "content": implementation, "artifact_role": "implementation"},
            {"relative_path": "tests/test_parse_powerpoint.py", "content": "def test_shape():\n    assert True\n", "artifact_role": "test"},
            {"relative_path": "fixtures/parse_powerpoint.txt", "content": "marker\n", "artifact_role": "fixture"},
        ]
    }


async def test_syntax_failure_never_becomes_verified(tmp_path):
    runtime = extension_runtime(tmp_path)
    bootstrap_construction(runtime)
    backend = FakeLLMBackend(default=proposal_response(_llm_artifacts("def nexus_capability(:\n    pass\n")))
    runtime.set_llm_construction_generator(LLMConstructionGenerator(backend))
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    await runtime.submit_event(reviewed(proposal.id, "approve"))

    plan = runtime.get_construction_plans(proposal.id)[0]
    assert runtime.get_construction_result(plan.id).status is ConstructionResultStatus.FAILED
    assert plan.target_capabilities == ["parse_powerpoint"]
    assert runtime.get_capability("parse_powerpoint") is None
    assert runtime.get_construction_trace(plan.id).llm_invocation is not None
    runtime.close()


async def test_missing_dependency_blocks_without_installing_it(tmp_path):
    runtime = extension_runtime(tmp_path)
    bootstrap_construction(runtime)
    source = "import package_that_does_not_exist_5b\n\ndef nexus_capability(fixture):\n    return {}\n"
    backend = FakeLLMBackend(default=proposal_response(_llm_artifacts(source)))
    runtime.set_llm_construction_generator(LLMConstructionGenerator(backend))
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    await runtime.submit_event(reviewed(proposal.id, "approve"))

    plan = runtime.get_construction_plans(proposal.id)[0]
    assert runtime.get_construction_result(plan.id).status is ConstructionResultStatus.BLOCKED
    assert plan.status is ConstructionPlanStatus.BLOCKED
    assert any("host installation is forbidden" in (c.failure_reason or "") for c in runtime.get_verification_checks(plan.id))
    runtime.close()


async def test_bounded_drain_and_restart_converge_on_one_plan(tmp_path):
    runtime = extension_runtime(tmp_path, "restart.db")
    bootstrap_construction(runtime)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    await runtime.submit_event(
        reviewed(proposal.id, "approve"),
        budget=DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1),
    )
    runtime.close()

    runtime = extension_runtime(tmp_path, "restart.db")
    bootstrap_construction(runtime)
    for _ in range(100):
        await runtime.run_pending(DrainBudget(max_dispatches=2, max_activations=2, max_cycles=2))
        plans = runtime.get_construction_plans(proposal.id)
        if plans and runtime.get_construction_result(plans[0].id) is not None:
            break
    plans = runtime.get_construction_plans(proposal.id)
    assert len(plans) == 1
    assert runtime.get_construction_result(plans[0].id).status is ConstructionResultStatus.VERIFIED
    assert len({a.idempotency_key for a in runtime.get_construction_trace(plans[0].id).actions}) == len(plans[0].expected_artifacts)
    runtime.close()


async def test_cancelled_work_closes_gap_proposal_and_review_continuation(tmp_path):
    runtime = extension_runtime(tmp_path)
    bootstrap_construction(runtime)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    waiter = runtime.continuation_store.all()[0]
    runtime.work_requirement_store.update_status(requirement.id, WorkStatus.CANCELLED)
    await runtime.submit_event(Event("work_cancelled", "test", {"work_requirement_id": str(requirement.id)}))

    assert runtime.get_capability_gap(proposal.capability_gap_id).status.value == "CANCELLED"
    assert runtime.get_extension_proposal(proposal.id).status.value == "CANCELLED"
    assert runtime.continuation_store.get(waiter.id) is None
    assert runtime.process_store.get_instance(waiter.process_instance_id).status.value == "COMPLETED"
    assert runtime.get_construction_plans(proposal.id) == []
    runtime.close()


async def test_add_process_definition_is_built_but_not_registered(tmp_path):
    runtime = extension_runtime(tmp_path)
    bootstrap_construction(runtime)
    definitions_before = {(d.name, d.version) for d in runtime.process_store.all_definitions()}
    requirement = gap_work(
        runtime,
        required=[needs("parse_csv_report", {"resource_type": "csv", "representation": "structure"})],
    )
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "ADD_PROCESS_DEFINITION"
    await runtime.submit_event(reviewed(proposal.id, "approve"))

    plan = runtime.get_construction_plans(proposal.id)[0]
    assert runtime.get_construction_result(plan.id).status is ConstructionResultStatus.VERIFIED
    assert {(d.name, d.version) for d in runtime.process_store.all_definitions()} == definitions_before
    assert runtime.get_capability("parse_csv_report") is None
    runtime.close()


async def test_backend_refuses_escape_even_after_action_approval_would_be_possible(tmp_path):
    runtime, _requirement, proposal = await _approved_runtime(tmp_path)
    plan = runtime.get_construction_plans(proposal.id)[0]
    workspace = runtime.construction_store.get_workspace_for_plan(plan.id)
    grant = runtime.construction_store.get_grant_for_plan(plan.id)
    # Re-open only this construction-scoped grant to exercise the final backend
    # check. It still grants no global or production permission.
    runtime.construction_store.update_grant_status(grant.id, "ACTIVE")
    outside = tmp_path / "production.py"
    backend = runtime.backends["construction_sandbox"]
    result = await backend.execute(ActionRequest(
        action_type="write_artifact", target=str(outside),
        parameters={
            "construction_plan_id": str(plan.id), "workspace_id": str(workspace.id),
            "relative_path": "../production.py", "content": "bad",
        },
        idempotency_key="escape-attempt",
    ))
    assert not result.success
    assert not result.retryable
    assert not outside.exists()
    runtime.close()


async def test_production_target_is_rejected_before_human_review(tmp_path):
    runtime, _requirement, proposal = await _approved_runtime(tmp_path)
    plan = runtime.get_construction_plans(proposal.id)[0]
    trace = runtime.get_construction_trace(plan.id)
    workspace = trace.workspace
    runtime.construction_store.update_grant_status(trace.grant.id, "ACTIVE")
    outside = tmp_path / "production_review.py"
    malicious = ActionProposal(
        backend="construction_sandbox", action_type="write_artifact",
        target=str(outside),
        parameters={
            "construction_plan_id": str(plan.id), "workspace_id": str(workspace.id),
            "relative_path": "../production_review.py", "content": "bad",
        },
        required_permissions=["sandbox.write"],
        declared_side_effects=["sandbox_write"], risk_level=RiskLevel.HIGH,
        created_by_process_id=trace.actions[0].created_by_process_id,
    )
    runtime.action_proposal_store.save(malicious)
    calls_before = len(runtime.backends["construction_sandbox"].calls)
    await runtime.submit_event(Event(
        "action_proposed", "test",
        {"action_proposal_id": str(malicious.id), "root_proposal_id": str(malicious.root_proposal_id)},
    ))
    assert runtime.get_action_proposal(malicious.id).status is ActionProposalStatus.REJECTED
    assert len(runtime.backends["construction_sandbox"].calls) == calls_before
    assert not outside.exists()
    assert not any(
        c.saved_process_state.get("action_proposal_id") == str(malicious.id)
        for c in runtime.continuation_store.all()
    )
    runtime.close()
