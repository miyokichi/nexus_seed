"""Phase 5C acceptance: reviewed exact artifacts become usable capabilities."""

from __future__ import annotations

import uuid

import pytest

from extension_helpers import (
    POWERPOINT,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_disabled_provider,
    register_capable,
    reviewed,
)

from nexus_seed.backends.action import ActionRequest
from nexus_seed.actions.models import ActionProposal, ActionProposalStatus, RiskLevel
from nexus_seed.adapters.manual import ManualAdapter
from nexus_seed.core.effects import EffectConflictError, check_conflicts
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessResult, ProcessStatus
from nexus_seed.resources.models import Resource, ResourceVersion
from nexus_seed.installation.models import (
    InstallationPlanStatus,
    InstallationResultStatus,
)
from nexus_seed.processes.installation import (
    INSTALLATION_REVIEWED,
    bootstrap_installation,
)
from nexus_seed.runtime.drain import DrainBudget
from nexus_seed.work.work_requirement import WorkStatus


async def _reviewed_installation(tmp_path, *, reuse=False, name="install.db"):
    runtime = extension_runtime(tmp_path, name)
    bootstrap_installation(runtime)
    if reuse:
        register_disabled_provider(runtime, "existing_reporter", "generate_report")
        requirement = gap_work(runtime, required=[needs("generate_report")])
    else:
        requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    await runtime.submit_event(reviewed(proposal.id, "approve"))
    plans = runtime.get_installation_plans()
    assert len(plans) == 1
    return runtime, requirement, proposal, plans[0]


def installation_reviewed(plan_id, decision="approve"):
    return Event(
        INSTALLATION_REVIEWED, "human",
        {"installation_plan_id": str(plan_id), "decision": decision},
    )


async def external_capability_request(ctx):
    """Test perception Process: turn one ingress Event into ordinary Work."""
    requirement = ctx.require_work(
        work_type="external_document_parse", work_key="external:deck-001",
        related_entities=["deck-001"], reason="external request",
        required_capabilities=[needs("parse_powerpoint", POWERPOINT)],
    )
    return ctx.complete(
        output={"work_requirement_id": str(requirement.id)},
        emitted_events=[ctx.new_event(
            "work_required", {"work_requirement_id": str(requirement.id)}
        )],
    )


def test_installation_effect_conflicts_are_refused():
    plan_id = uuid.uuid4()
    result = ProcessResult(
        status=ProcessStatus.COMPLETED,
        installation_plan_updates=[
            (plan_id, "INSTALLED", []),
            (plan_id, "ACTIVATED", []),
        ],
    )
    with pytest.raises(EffectConflictError, match="installation plan"):
        check_conflicts(result)


async def test_full_ingress_5a_5b_5c_loop_returns_to_original_work(tmp_path):
    runtime = extension_runtime(tmp_path, "full-loop.db")
    bootstrap_installation(runtime)
    runtime.register_process(ProcessDefinition(
        name="external_capability_request", version="1",
        handler="external_capability_request",
        trigger_event_types=("external_capability_request",),
    ), external_capability_request)
    envelope = ManualAdapter("phase5c-test").envelope(
        event_type="external_capability_request",
        source_event_key="external-request-001",
        payload={"resource": "deck-001"},
    )
    ingested = await runtime.ingress.ingest(envelope)
    proposal = only_proposal(runtime)
    requirement = runtime.get_work_requirements()[0]
    assert ingested.event.ingress_receipt_id is not None
    assert requirement.status is WorkStatus.BLOCKED_CAPABILITY

    await runtime.submit_event(reviewed(proposal.id, "approve"))
    plan = runtime.get_installation_plans()[0]
    await runtime.submit_event(installation_reviewed(plan.id))

    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ACTIVATED
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    assert only_gap(runtime).status.value == "RESOLVED"
    assert runtime.get_pending_event_delivery_count() == 0
    trace = runtime.get_installation_trace(plan.id)
    assert trace.work_requirement.id == requirement.id
    assert trace.construction_result.status.value == "VERIFIED"
    assert trace.activation.status.value == "ACTIVE"
    runtime.close()


async def test_add_extractor_install_smoke_activate_and_resume_work(tmp_path):
    runtime, requirement, _proposal, plan = await _reviewed_installation(tmp_path)
    assert plan.status is InstallationPlanStatus.REVIEW, plan.validation_reasons
    assert runtime.get_capability("parse_powerpoint") is None

    await runtime.submit_event(installation_reviewed(plan.id))

    plan = runtime.get_installation_plan(plan.id)
    result = runtime.get_installation_result(plan.id)
    activation = runtime.get_activation_record(plan.id)
    assert plan.status is InstallationPlanStatus.ACTIVATED
    assert result.status is InstallationResultStatus.ACTIVATED
    assert activation is not None
    assert runtime.get_capability("parse_powerpoint") is not None
    assert runtime.extractors.get(plan.component_name) is not None
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    assert only_gap(runtime).status.value == "RESOLVED"
    assert runtime.get_pending_event_delivery_count() == 0
    trace = runtime.get_installation_trace(plan.id)
    assert trace.activation.artifact_hashes == [a.content_hash for a in plan.artifact_versions]
    assert trace.grant.status.value == "REVOKED"
    runtime.close()


async def test_no_capability_before_production_smoke(tmp_path):
    runtime, _requirement, _proposal, plan = await _reviewed_installation(tmp_path)
    assert plan.status is InstallationPlanStatus.REVIEW
    assert runtime.get_capability("parse_powerpoint") is None
    await runtime.submit_event(
        installation_reviewed(plan.id),
        budget=DrainBudget(max_dispatches=7, max_activations=7, max_cycles=7),
    )
    # Regardless of the precise slice boundary, ACTIVE implies all smoke checks;
    # every earlier durable state must remain undiscoverable.
    current = runtime.get_installation_plan(plan.id)
    if current.status is not InstallationPlanStatus.ACTIVATED:
        assert runtime.get_capability("parse_powerpoint") is None
    runtime.close()


async def test_hash_mutation_is_rejected_before_install(tmp_path):
    runtime, _requirement, _proposal, plan = await _reviewed_installation(tmp_path)
    artifact = plan.artifact_versions[0]
    with open(artifact.locator, "a", encoding="utf-8") as stream:
        stream.write("\nmutated after verification\n")
    await runtime.submit_event(installation_reviewed(plan.id))
    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.FAILED
    assert runtime.get_installation_grant(plan.id) is None
    assert runtime.get_capability("parse_powerpoint") is None
    runtime.close()


async def test_backend_rejects_destination_outside_grant(tmp_path):
    runtime, _requirement, _proposal, plan = await _reviewed_installation(tmp_path)
    await runtime.submit_event(
        installation_reviewed(plan.id),
        budget=DrainBudget(max_dispatches=2, max_activations=2, max_cycles=2),
    )
    grant = runtime.get_installation_grant(plan.id)
    assert grant is not None
    artifact = plan.artifact_versions[0]
    result = await runtime.backends["installation_production"].execute(ActionRequest(
        action_type="copy_verified_artifact", target="../escape.py",
        parameters={
            "installation_plan_id": str(plan.id),
            "resource_version_id": str(artifact.resource_version_id),
            "content_hash": artifact.content_hash,
            "source_locator": artifact.locator,
            "destination": "../escape.py",
        },
        idempotency_key="malicious-install",
    ))
    assert not result.success
    assert "outside the InstallationGrant" in result.error
    runtime.close()


async def test_register_existing_process_enables_exact_provider(tmp_path):
    runtime, requirement, _proposal, plan = await _reviewed_installation(tmp_path, reuse=True)
    assert not runtime.get_capability("generate_report", "1").enabled
    await runtime.submit_event(installation_reviewed(plan.id))
    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ACTIVATED, (runtime.get_installation_result(plan.id).failure_reason, plan.registry_changes)
    assert runtime.get_capability("generate_report", "1").enabled
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    runtime.close()


async def test_reject_creates_no_grant_or_action(tmp_path):
    runtime, _requirement, _proposal, plan = await _reviewed_installation(tmp_path)
    before = len(runtime.get_action_proposals())
    await runtime.submit_event(installation_reviewed(plan.id, "reject"))
    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.CANCELLED
    assert runtime.get_installation_grant(plan.id) is None
    assert len(runtime.get_action_proposals()) == before
    runtime.close()


async def test_add_process_definition_becomes_active_provider(tmp_path):
    runtime = extension_runtime(tmp_path, "process-install.db")
    bootstrap_installation(runtime)
    requirement = gap_work(runtime, required=[needs(
        "parse_csv_report", {"resource_type": "csv", "representation": "structure"}
    )])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    assert proposal.declared_strategy == "ADD_PROCESS_DEFINITION"
    await runtime.submit_event(reviewed(proposal.id, "approve"))
    plan = runtime.get_installation_plans()[0]
    await runtime.submit_event(installation_reviewed(plan.id))

    activation = runtime.get_activation_record(plan.id)
    definition = runtime.process_store.get_definition(
        activation.definition_name, activation.definition_version
    )
    assert plan.strategy == "ADD_PROCESS_DEFINITION"
    assert definition.handler == "installed_extension_handler"
    assert runtime.get_capability("parse_csv_report") is not None
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    runtime.close()


async def test_smoke_failure_rolls_back_v2_and_preserves_v1(tmp_path):
    runtime, _requirement, _proposal, plan = await _reviewed_installation(
        tmp_path, name="rollback.db"
    )
    register_capable(runtime, "legacy_powerpoint", ("parse_powerpoint",), version="1")
    await runtime.submit_event(
        installation_reviewed(plan.id),
        budget=DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1),
    )
    for _ in range(80):
        current = runtime.get_installation_plan(plan.id)
        if current.status is InstallationPlanStatus.INSTALLED:
            break
        await runtime.run_pending(DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1))
    current = runtime.get_installation_plan(plan.id)
    assert current.status is InstallationPlanStatus.INSTALLED
    implementation = next(
        a for a in current.artifact_versions if a.artifact_role == "implementation"
    )
    installed = runtime.installation_manager.resolve(implementation.destination)
    installed.write_text("def nexus_capability(:\n", encoding="utf-8")

    for _ in range(80):
        await runtime.run_pending(DrainBudget(max_dispatches=2, max_activations=2, max_cycles=2))
        if runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ROLLED_BACK:
            break
    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ROLLED_BACK
    assert runtime.get_installation_result(plan.id).status is InstallationResultStatus.ROLLED_BACK
    assert runtime.get_capability("parse_powerpoint", "1").enabled
    assert runtime.capabilities.get_processes_providing("parse_powerpoint", "1") == [("legacy_powerpoint", "1")]
    assert not runtime.installation_manager.component_root(plan.component_name, plan.component_version).exists()
    assert runtime.installation_store.rollback_for_plan(plan.id).status.value == "COMPLETED"
    runtime.close()


async def test_restart_and_small_budgets_converge_once(tmp_path):
    name = "install-restart.db"
    runtime, requirement, _proposal, plan = await _reviewed_installation(tmp_path, name=name)
    runtime.close()

    for iteration in range(140):
        runtime = extension_runtime(tmp_path, name)
        bootstrap_installation(runtime)
        if iteration == 0:
            await runtime.submit_event(
                installation_reviewed(plan.id),
                budget=DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1),
            )
        else:
            await runtime.run_pending(
                DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1)
            )
        done = (
            runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ACTIVATED
            and runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
            and runtime.get_pending_event_delivery_count() == 0
        )
        if done:
            break
        runtime.close()
    assert done
    assert len(runtime.get_installation_plans()) == 1
    assert len(runtime.installation_store.active_activations()) == 1
    assert runtime.get_installation_result(plan.id).status is InstallationResultStatus.ACTIVATED
    assert len(runtime.get_capability_gaps_for_work(requirement.id)) == 1
    copy_actions = [
        action for action in runtime.get_installation_trace(plan.id).production_actions
        if action.action_type == "copy_verified_artifact"
    ]
    assert len(copy_actions) == len({action.idempotency_key for action in copy_actions})
    runtime.close()


async def test_nonconstruction_resource_and_arbitrary_action_are_refused(tmp_path):
    runtime, _requirement, _proposal, plan = await _reviewed_installation(
        tmp_path, name="security.db"
    )
    await runtime.submit_event(
        installation_reviewed(plan.id),
        budget=DrainBudget(max_dispatches=2, max_activations=2, max_cycles=2),
    )
    grant = runtime.get_installation_grant(plan.id)
    artifact = plan.artifact_versions[0]
    unrelated = Resource(uri="test://not-construction", resource_type="python")
    unrelated_version = ResourceVersion(
        resource_id=unrelated.id, content_hash=artifact.content_hash,
        locator=artifact.locator,
    )
    unrelated.current_version_id = unrelated_version.id
    runtime.resource_store.save_resource(unrelated)
    runtime.resource_store.save_version(unrelated_version)
    backend = runtime.backends["installation_production"]
    rejected = await backend.execute(ActionRequest(
        action_type="copy_verified_artifact", target=artifact.destination,
        parameters={
            "installation_plan_id": str(plan.id),
            "installation_grant_id": str(grant.id),
            "resource_version_id": str(unrelated_version.id),
            "content_hash": artifact.content_hash,
            "source_locator": artifact.locator,
            "destination": artifact.destination,
        }, idempotency_key="not-a-construction-version",
    ))
    assert not rejected.success
    assert "not one of the verified construction artifacts" in rejected.error

    # Even a process that legitimately performed the reviewed install cannot
    # turn that approval into another destination or a reviewable exception.
    for _ in range(80):
        await runtime.run_pending(DrainBudget(max_dispatches=2, max_activations=2, max_cycles=2))
        if runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ACTIVATED:
            break
    creator = next(
        value for value in runtime.process_store.all_instances()
        if value.definition_name == "install_extension"
    )
    malicious = ActionProposal(
        backend="installation_production", action_type="copy_verified_artifact",
        target="other/version/runtime.py",
        parameters={
            "installation_plan_id": str(plan.id),
            "installation_grant_id": str(grant.id),
            "resource_version_id": str(artifact.resource_version_id),
            "content_hash": artifact.content_hash,
            "source_locator": artifact.locator,
            "destination": "other/version/runtime.py",
        },
        required_permissions=["production.install"],
        declared_side_effects=["filesystem_write"], risk_level=RiskLevel.HIGH,
        created_by_process_id=creator.id,
    )
    runtime.action_proposal_store.save(malicious)
    calls_before = len(backend.calls)
    await runtime.submit_event(Event(
        "action_proposed", "test",
        {"action_proposal_id": str(malicious.id), "root_proposal_id": str(malicious.root_proposal_id)},
    ))
    assert runtime.get_action_proposal(malicious.id).status is ActionProposalStatus.REJECTED
    assert len(backend.calls) == calls_before
    assert not any(
        continuation.saved_process_state.get("action_proposal_id") == str(malicious.id)
        for continuation in runtime.continuation_store.all()
    )
    runtime.close()


async def test_core_policy_shell_and_network_plan_changes_are_invalid(tmp_path):
    runtime, _requirement, proposal, plan = await _reviewed_installation(tmp_path)
    plan.registry_changes[0]["modifies_core"] = True
    plan.registry_changes[0]["modifies_policy"] = True
    plan.required_permissions.extend(["shell.execute", "network.unrestricted"])
    construction_result = runtime.construction_store.get_result(plan.construction_result_id)
    construction_plan = runtime.construction_store.get_plan(construction_result.construction_plan_id)
    workspace = runtime.construction_store.get_workspace_for_plan(construction_plan.id)
    validation = runtime.installation_validator.validate(
        plan, construction_result=construction_result, construction_plan=construction_plan,
        proposal=proposal, workspace=workspace, resource_store=runtime.resource_store,
        production_manager=runtime.installation_manager,
    )
    assert not validation.ok
    assert any("forbidden installation permission" in reason for reason in validation.reasons)
    assert any("runtime core or permission policy" in reason for reason in validation.reasons)
    runtime.close()


async def test_rollback_resumes_after_restart_and_is_idempotent(tmp_path):
    name = "rollback-restart.db"
    runtime, _requirement, _proposal, plan = await _reviewed_installation(tmp_path, name=name)
    await runtime.submit_event(
        installation_reviewed(plan.id),
        budget=DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1),
    )
    for _ in range(80):
        if runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.INSTALLED:
            break
        await runtime.run_pending(DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1))
    current = runtime.get_installation_plan(plan.id)
    implementation = next(a for a in current.artifact_versions if a.artifact_role == "implementation")
    runtime.installation_manager.resolve(implementation.destination).write_text(
        "def nexus_capability(:\n", encoding="utf-8"
    )
    for _ in range(40):
        await runtime.run_pending(DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1))
        if runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ROLLING_BACK:
            break
    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ROLLING_BACK
    runtime.close()

    runtime = extension_runtime(tmp_path, name)
    bootstrap_installation(runtime)
    for _ in range(80):
        await runtime.run_pending(DrainBudget(max_dispatches=1, max_activations=1, max_cycles=1))
        if runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ROLLED_BACK:
            break
    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ROLLED_BACK
    rollback_actions = [
        action for action in runtime.get_installation_trace(plan.id).production_actions
        if action.action_type == "rollback_installation"
    ]
    assert len(rollback_actions) == 1
    await runtime.run_pending()
    assert runtime.get_installation_plan(plan.id).status is InstallationPlanStatus.ROLLED_BACK
    runtime.close()
