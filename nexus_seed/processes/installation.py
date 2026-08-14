"""Ordinary Processes implementing Phase 5C production promotion."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from ..actions.models import RiskLevel
from ..capabilities.models import Capability
from ..construction.models import ConstructionResultStatus
from ..core.event import utcnow
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..extension.models import CapabilityGapStatus, ExtensionStrategy
from ..installation.models import (
    ActivationRecord,
    InstallationCheck,
    InstallationCheckStatus,
    InstallationDecision,
    InstallationDecisionRecord,
    InstallationGrant,
    InstallationGrantStatus,
    InstallationPlanStatus,
    InstallationResult,
    InstallationResultStatus,
    InstallationStepStatus,
    InstallationStepType,
    RollbackRecord,
    RollbackStatus,
)
from ..installation.validator import sha256_file
from ..installation.workspace import BACKEND_NAME
from ..work.work_requirement import WorkStatus
from .actions import action_proposed_event, bootstrap_actions, waiting_for_action
from .construction import bootstrap_construction


EXTENSION_CONSTRUCTION_VERIFIED = "extension_construction_verified"
INSTALLATION_REVIEWED = "installation_reviewed"
INSTALLATION_REVIEW_REQUIRED = "installation_review_required"
INSTALLATION_APPROVED = "installation_approved"
INSTALLATION_REJECTED = "installation_rejected"
EXTENSION_INSTALLED = "extension_installed"
INSTALLATION_VERIFIED = "extension_installation_verified"
INSTALLATION_ROLLBACK_REQUESTED = "installation_rollback_requested"
INSTALLATION_ROLLED_BACK = "installation_rolled_back"
EXTENSION_ACTIVATED = "extension_activated"


def _uuid(value):
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, TypeError):
        return None


def _payload(plan, **extra) -> dict:
    value = {
        "installation_plan_id": str(plan.id),
        "construction_result_id": str(plan.construction_result_id),
        "extension_proposal_id": str(plan.extension_proposal_id),
        "capability_gap_id": str(plan.capability_gap_id),
        "work_requirement_id": str(plan.work_requirement_id),
    }
    value.update(extra)
    return value


def _dependencies(ctx, plan):
    result = ctx.services.get_construction_result_by_id(plan.construction_result_id)
    construction_plan = (
        ctx.services.get_construction_plan(result.construction_plan_id) if result else None
    )
    proposal = ctx.services.get_extension_proposal(plan.extension_proposal_id)
    workspace = (
        ctx.services.get_sandbox_workspace_for_plan(construction_plan.id)
        if construction_plan else None
    )
    return result, construction_plan, proposal, workspace


def _validate(ctx, plan):
    result, construction_plan, proposal, workspace = _dependencies(ctx, plan)
    validation = ctx.services.get_installation_validator().validate(
        plan,
        construction_result=result,
        construction_plan=construction_plan,
        proposal=proposal,
        workspace=workspace,
        resource_store=ctx.services.get_resource_store(),
        production_manager=ctx.services.get_installation_manager(),
    )
    return validation, result, construction_plan, proposal, workspace


async def plan_extension_installation(ctx: ProcessContext) -> ProcessResult:
    """Build/revalidate one exact plan and wait for explicit production review."""
    if ctx.resume_point == "await_installation_review":
        return _review_installation(ctx)
    assert ctx.event is not None and ctx.services is not None
    result_id = _uuid(ctx.event.payload.get("construction_result_id"))
    construction_result = ctx.services.get_construction_result_by_id(result_id)
    if construction_result is None or construction_result.status is not ConstructionResultStatus.VERIFIED:
        return ctx.complete(output={"planned": False, "reason": "construction is not VERIFIED"})
    existing = ctx.services.get_installation_plan_for_result(construction_result.id)
    if existing is not None:
        return ctx.complete(output={"planned": False, "reason": "logical plan already exists", "installation_plan_id": str(existing.id)})
    construction_plan = ctx.services.get_construction_plan(construction_result.construction_plan_id)
    proposal = ctx.services.get_extension_proposal(construction_result.extension_proposal_id)
    workspace = ctx.services.get_sandbox_workspace_for_plan(construction_plan.id) if construction_plan else None
    if construction_plan is None or proposal is None or workspace is None:
        return ctx.fail("construction provenance is incomplete")
    plan = ctx.services.get_installation_planner().build(
        construction_result, construction_plan, proposal,
        resource_store=ctx.services.get_resource_store(), workspace=workspace,
        capability_registry=ctx.services.get_capability_registry(),
        installation_store=ctx.services.get_installation_store(),
        context_snapshot_id=ctx.context_snapshot_id,
    )
    validation = ctx.services.get_installation_validator().validate(
        plan, construction_result=construction_result, construction_plan=construction_plan,
        proposal=proposal, workspace=workspace,
        resource_store=ctx.services.get_resource_store(),
        production_manager=ctx.services.get_installation_manager(),
    )
    plan.validation_reasons = list(validation.reasons)
    if not validation.ok:
        plan.status = InstallationPlanStatus.FAILED
        ctx.record_installation_plan(plan)
        ctx.record_installation_result(InstallationResult(
            installation_plan_id=plan.id, status=InstallationResultStatus.FAILED,
            failure_reason="; ".join(validation.reasons),
            evidence={"validation": validation.reasons},
        ))
        return ctx.complete(
            output={"planned": False, "reasons": validation.reasons},
            emitted_events=[ctx.new_event(INSTALLATION_REJECTED, _payload(plan, reasons=validation.reasons))],
        )
    policy_decision = ctx.services.get_installation_policy().decide(plan, validation)
    if policy_decision is InstallationDecision.REJECT:
        plan.status = InstallationPlanStatus.FAILED
        plan.validation_reasons = ["installation strategy rejected by policy"]
        ctx.record_installation_plan(plan)
        ctx.record_installation_decision(InstallationDecisionRecord(
            installation_plan_id=plan.id, decision=InstallationDecision.REJECT,
            decided_by_process_id=ctx.instance.id,
            reasons=list(plan.validation_reasons),
        ))
        ctx.record_installation_result(InstallationResult(
            installation_plan_id=plan.id, status=InstallationResultStatus.FAILED,
            failure_reason="installation strategy rejected by policy",
            evidence={"policy": ctx.services.get_installation_policy().to_dict()},
        ))
        return ctx.complete(
            output={"planned": False, "reasons": plan.validation_reasons},
            emitted_events=[ctx.new_event(INSTALLATION_REJECTED, _payload(plan, reasons=plan.validation_reasons))],
        )
    plan.status = InstallationPlanStatus.REVIEW
    ctx.record_installation_plan(plan)
    ctx.record_installation_decision(InstallationDecisionRecord(
        installation_plan_id=plan.id, decision=InstallationDecision.REVIEW,
        decided_by_process_id=ctx.instance.id,
        reasons=["InstallationPolicy requires human production review"],
    ))
    return ctx.suspend(
        resume_point="await_installation_review",
        waiting_for={"event_type": INSTALLATION_REVIEWED, "installation_plan_id": str(plan.id)},
        saved_process_state={"installation_plan_id": str(plan.id)},
        emitted_events=[ctx.new_event(INSTALLATION_REVIEW_REQUIRED, _payload(plan))],
    )


def _review_installation(ctx: ProcessContext) -> ProcessResult:
    assert ctx.event is not None and ctx.services is not None
    plan_id = _uuid(ctx.event.payload.get("installation_plan_id")) or _uuid(
        ctx.saved_process_state.get("installation_plan_id")
    )
    plan = ctx.services.get_installation_plan(plan_id)
    if plan is None:
        return ctx.fail("installation plan disappeared during review")
    if plan.status is not InstallationPlanStatus.REVIEW:
        return ctx.complete(output={"approved": False, "reason": f"plan already {plan.status.value}"})
    decision = str(ctx.event.payload.get("decision", "reject")).lower()
    if decision != "approve":
        ctx.update_installation_plan(plan.id, InstallationPlanStatus.CANCELLED, reasons=["rejected by human review"])
        ctx.record_installation_decision(InstallationDecisionRecord(
            installation_plan_id=plan.id, decision=InstallationDecision.REJECT,
            decided_by_process_id=ctx.instance.id, reasons=["rejected by human review"],
            reviewed_by_event_id=ctx.event.id,
        ))
        ctx.record_installation_result(InstallationResult(
            installation_plan_id=plan.id, status=InstallationResultStatus.CANCELLED,
            failure_reason="rejected by human review",
        ))
        return ctx.complete(
            output={"approved": False},
            emitted_events=[ctx.new_event(INSTALLATION_REJECTED, _payload(plan))],
        )
    validation, *_ = _validate(ctx, plan)
    if not validation.ok:
        ctx.update_installation_plan(plan.id, InstallationPlanStatus.FAILED, reasons=validation.reasons)
        ctx.record_installation_decision(InstallationDecisionRecord(
            installation_plan_id=plan.id, decision=InstallationDecision.REJECT,
            decided_by_process_id=ctx.instance.id, reasons=validation.reasons,
            reviewed_by_event_id=ctx.event.id,
        ))
        ctx.record_installation_result(InstallationResult(
            installation_plan_id=plan.id, status=InstallationResultStatus.FAILED,
            failure_reason="; ".join(validation.reasons), evidence={"revalidation": validation.reasons},
        ))
        return ctx.complete(output={"approved": False, "reasons": validation.reasons})
    grant = InstallationGrant(
        installation_plan_id=plan.id,
        allowed_artifact_hashes=[a.content_hash for a in plan.artifact_versions],
        allowed_destinations=list(plan.production_destinations),
        allowed_registry_changes=list(plan.registry_changes),
        allowed_permissions=["production.install", "production.read", "capability.enable"],
    )
    ctx.update_installation_plan(plan.id, InstallationPlanStatus.APPROVED)
    ctx.record_installation_grant(grant)
    ctx.record_installation_decision(InstallationDecisionRecord(
        installation_plan_id=plan.id, decision=InstallationDecision.APPROVE,
        decided_by_process_id=ctx.instance.id, reviewed_by_event_id=ctx.event.id,
    ))
    return ctx.complete(
        output={"approved": True, "installation_plan_id": str(plan.id), "grant_id": str(grant.id)},
        emitted_events=[ctx.new_event(INSTALLATION_APPROVED, _payload(plan, installation_grant_id=str(grant.id)))],
    )


async def install_extension(ctx: ProcessContext) -> ProcessResult:
    """Copy every exact artifact through the existing Action boundary."""
    if ctx.resume_point == "await_installation_action":
        return _resume_installation_action(ctx)
    assert ctx.event is not None and ctx.services is not None
    plan = ctx.services.get_installation_plan(_uuid(ctx.event.payload.get("installation_plan_id")))
    if plan is None:
        return ctx.fail("installation plan not found")
    if plan.status not in {InstallationPlanStatus.APPROVED, InstallationPlanStatus.INSTALLING}:
        return ctx.complete(output={"installed": False, "reason": f"plan is {plan.status.value}"})
    grant = ctx.services.get_installation_grant_for_plan(plan.id)
    if grant is None or grant.status is not InstallationGrantStatus.ACTIVE:
        return ctx.complete(output={"installed": False, "reason": "InstallationGrant is not active"})
    validation, *_ = _validate(ctx, plan)
    if not validation.ok:
        return _installation_failed(ctx, plan, "; ".join(validation.reasons))
    ctx.update_installation_plan(plan.id, InstallationPlanStatus.INSTALLING)
    return _propose_copy(ctx, plan, grant, 0)


def _propose_copy(ctx, plan, grant, index):
    artifact = plan.artifact_versions[index]
    copy_steps = [s for s in plan.steps if s.step_type is InstallationStepType.COPY_VERIFIED_ARTIFACT]
    ctx.update_installation_step(copy_steps[index].id, InstallationStepStatus.RUNNING)
    proposal = ctx.propose_action(
        backend=BACKEND_NAME, action_type="copy_verified_artifact",
        target=artifact.destination,
        parameters={
            "installation_plan_id": str(plan.id), "installation_grant_id": str(grant.id),
            "resource_version_id": str(artifact.resource_version_id),
            "content_hash": artifact.content_hash, "source_locator": artifact.locator,
            "destination": artifact.destination,
        },
        required_permissions=["production.install"], declared_side_effects=["filesystem_write"],
        risk_level=RiskLevel.LOW, rationale="copy one exact human-approved verified artifact",
        idempotency_key=f"installation:{plan.id}:copy:{artifact.resource_version_id}",
    )
    return ctx.suspend(
        resume_point="await_installation_action", waiting_for=waiting_for_action(proposal),
        saved_process_state={"installation_plan_id": str(plan.id), "grant_id": str(grant.id), "artifact_index": index},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


def _resume_installation_action(ctx: ProcessContext) -> ProcessResult:
    assert ctx.event is not None and ctx.services is not None
    plan = ctx.services.get_installation_plan(_uuid(ctx.saved_process_state.get("installation_plan_id")))
    if plan is None:
        return ctx.fail("installation plan disappeared while awaiting copy")
    grant = ctx.services.get_installation_grant_for_plan(plan.id)
    if grant is None:
        return ctx.fail("InstallationGrant disappeared while awaiting copy")
    index = int(ctx.saved_process_state.get("artifact_index", 0))
    copy_steps = [s for s in plan.steps if s.step_type is InstallationStepType.COPY_VERIFIED_ARTIFACT]
    if ctx.event.type != "action_succeeded":
        ctx.update_installation_step(copy_steps[index].id, InstallationStepStatus.FAILED)
        return _installation_failed(ctx, plan, f"production copy ended as {ctx.event.type}")
    ctx.update_installation_step(copy_steps[index].id, InstallationStepStatus.COMPLETED)
    if index + 1 < len(plan.artifact_versions):
        return _propose_copy(ctx, plan, grant, index + 1)
    result = InstallationResult(
        installation_plan_id=plan.id, status=InstallationResultStatus.INSTALLED,
        installed_artifact_versions=[a.resource_version_id for a in plan.artifact_versions],
        previous_state=dict((plan.rollback_spec or {}).get("previous_state") or {}),
        evidence={"destinations": list(plan.production_destinations)},
    )
    ctx.update_installation_plan(plan.id, InstallationPlanStatus.INSTALLED)
    ctx.record_installation_result(result)
    return ctx.complete(
        output={"installed": True, "installation_plan_id": str(plan.id)},
        emitted_events=[ctx.new_event(EXTENSION_INSTALLED, _payload(plan))],
    )


def _installation_failed(ctx, plan, reason) -> ProcessResult:
    existing = ctx.services.get_installation_result(plan.id)
    result = InstallationResult(
        installation_plan_id=plan.id, status=InstallationResultStatus.FAILED,
        installed_artifact_versions=list(existing.installed_artifact_versions) if existing else [],
        previous_state=dict((plan.rollback_spec or {}).get("previous_state") or {}),
        failure_reason=reason,
        id=existing.id if existing else uuid.uuid4(), created_at=existing.created_at if existing else utcnow(),
    )
    ctx.update_installation_plan(plan.id, InstallationPlanStatus.FAILED, reasons=[reason])
    ctx.record_installation_result(result)
    return ctx.complete(
        output={"installed": False, "reason": reason},
        emitted_events=[ctx.new_event(INSTALLATION_ROLLBACK_REQUESTED, _payload(plan, reason=reason))],
    )


async def verify_installed_extension(ctx: ProcessContext) -> ProcessResult:
    """Verify the production copy without publishing any capability."""
    assert ctx.event is not None and ctx.services is not None
    plan = ctx.services.get_installation_plan(_uuid(ctx.event.payload.get("installation_plan_id")))
    if plan is None:
        return ctx.fail("installed plan not found")
    if plan.status is not InstallationPlanStatus.INSTALLED:
        return ctx.complete(output={"verified": False, "reason": f"plan is {plan.status.value}"})
    existing = ctx.services.get_installation_result(plan.id)
    reasons: list[str] = []
    checks: list[InstallationCheck] = []
    manager = ctx.services.get_installation_manager()
    hash_evidence = {}
    for artifact in plan.artifact_versions:
        try:
            destination = manager.resolve(artifact.destination, write=False)
            actual = sha256_file(destination)
            hash_evidence[artifact.destination] = actual
            if actual != artifact.content_hash:
                reasons.append(f"installed hash mismatch: {artifact.relative_path}")
        except OSError as exc:
            reasons.append(f"installed artifact unavailable: {artifact.relative_path}: {exc}")
    checks.append(_check(plan, "artifact_hash", "ARTIFACT_HASH", not reasons, hash_evidence, reasons[0] if reasons else None))
    change = plan.registry_changes[0] if plan.registry_changes else {}
    temporary = SimpleNamespace(
        id=uuid.uuid5(plan.id, "smoke"), component_name=plan.component_name,
        component_version=plan.component_version,
        installed_root=str(manager.component_root(plan.component_name, plan.component_version)),
    )
    load_reason = None
    interface_reason = None
    smoke_reason = None
    load_evidence = {}
    smoke_evidence = {}
    if plan.strategy == ExtensionStrategy.REGISTER_EXISTING_PROCESS.value:
        definition = ctx.services.get_definition(change.get("definition_name", ""), change.get("definition_version", ""))
        if definition is None:
            load_reason = "existing ProcessDefinition is unavailable"
        else:
            load_evidence = {"definition": f"{definition.name}:{definition.version}"}
            if not definition.handler:
                interface_reason = "existing ProcessDefinition has no handler"
            smoke_evidence = {"definition_available": True}
    else:
        try:
            function = manager.capability_function(temporary)
            load_evidence = {"module_loadable": True}
        except (OSError, ImportError, SyntaxError, AttributeError, ValueError, TypeError) as exc:
            load_reason = str(exc)
            function = None
        if function is not None and not callable(function):
            interface_reason = "nexus_capability is not callable"
        if function is not None and interface_reason is None:
            try:
                output = function({"phase": "production_smoke", "side_effects": False})
                if output is None:
                    raise ValueError("production smoke returned no output")
                smoke_evidence = {"output_type": type(output).__name__}
            except (ValueError, TypeError, RuntimeError) as exc:
                smoke_reason = str(exc)
    if load_reason:
        reasons.append(load_reason)
    if interface_reason:
        reasons.append(interface_reason)
    if smoke_reason:
        reasons.append(smoke_reason)
    checks.append(_check(plan, "module_load", "MODULE_LOAD", load_reason is None, load_evidence, load_reason))
    checks.append(_check(plan, "expected_interface", "EXPECTED_INTERFACE", load_reason is None and interface_reason is None, {"interface": "nexus_capability" if plan.strategy != ExtensionStrategy.REGISTER_EXISTING_PROCESS.value else "ProcessDefinition.handler"}, interface_reason or load_reason))
    checks.append(_check(plan, "capability_smoke", "CAPABILITY_SMOKE", load_reason is None and interface_reason is None and smoke_reason is None, smoke_evidence, smoke_reason or interface_reason or load_reason))
    metadata_reason = None if plan.registry_changes and change.get("strategy") == plan.strategy else "manifest/registry metadata does not match the reviewed plan"
    if metadata_reason:
        reasons.append(metadata_reason)
    checks.append(_check(plan, "manifest_metadata", "MANIFEST_METADATA", metadata_reason is None, {"registry_changes": plan.registry_changes}, metadata_reason))
    proposal = _dependencies(ctx, plan)[2]
    permission_reason = None
    if proposal is None or set(plan.required_permissions) - set(proposal.required_permissions):
        permission_reason = "permission declaration exceeds the reviewed proposal"
        reasons.append(permission_reason)
    checks.append(_check(plan, "permission_declaration", "PERMISSION_DECLARATION", permission_reason is None, {"required_permissions": plan.required_permissions}, permission_reason))
    for check in checks:
        ctx.record_installation_check(check)
    smoke_step = next((s for s in plan.steps if s.step_type is InstallationStepType.RUN_SMOKE_TEST), None)
    if smoke_step:
        ctx.update_installation_step(smoke_step.id, InstallationStepStatus.COMPLETED if not reasons else InstallationStepStatus.FAILED)
    if reasons:
        return _installation_failed(ctx, plan, "; ".join(reasons))
    if existing is None:
        return ctx.fail("installation result missing before smoke verification")
    existing.evidence = {**existing.evidence, "post_install_checks": [str(c.id) for c in checks]}
    ctx.record_installation_result(existing)
    return ctx.complete(
        output={"verified": True, "installation_plan_id": str(plan.id)},
        emitted_events=[ctx.new_event(INSTALLATION_VERIFIED, _payload(plan))],
    )


def _check(plan, key, kind, passed, evidence, failure):
    return InstallationCheck(
        installation_plan_id=plan.id, check_key=key, check_type=kind,
        status=InstallationCheckStatus.PASS if passed else InstallationCheckStatus.FAIL,
        evidence=evidence, failure_reason=failure, completed_at=utcnow(),
        id=uuid.uuid5(plan.id, f"installation-check:{key}"),
    )


async def activate_installed_extension(ctx: ProcessContext) -> ProcessResult:
    """Atomically expose a smoke-verified component and its capabilities."""
    assert ctx.event is not None and ctx.services is not None
    plan = ctx.services.get_installation_plan(_uuid(ctx.event.payload.get("installation_plan_id")))
    if plan is None:
        return ctx.fail("verified installation plan not found")
    existing_activation = ctx.services.get_installation_store().activation_for_plan(plan.id)
    if existing_activation is not None:
        return ctx.complete(output={"activated": True, "reason": "already active"})
    result = ctx.services.get_installation_result(plan.id)
    grant = ctx.services.get_installation_grant_for_plan(plan.id)
    checks = ctx.services.get_installation_store().checks_for_plan(plan.id)
    if (
        plan.status is not InstallationPlanStatus.INSTALLED
        or result is None or result.status is not InstallationResultStatus.INSTALLED
        or grant is None or grant.status is not InstallationGrantStatus.ACTIVE
        or not checks or any(c.required and c.status is not InstallationCheckStatus.PASS for c in checks)
    ):
        return ctx.complete(output={"activated": False, "reason": "activation prerequisites are incomplete"})
    validation, *_ = _validate(ctx, plan)
    if not validation.ok:
        return _installation_failed(ctx, plan, "; ".join(validation.reasons))
    manager = ctx.services.get_installation_manager()
    for artifact in plan.artifact_versions:
        try:
            if sha256_file(manager.resolve(artifact.destination, write=False)) != artifact.content_hash:
                return _installation_failed(ctx, plan, "artifact mutated between smoke verification and activation")
        except OSError as exc:
            return _installation_failed(ctx, plan, f"installed artifact disappeared before activation: {exc}")
    change = plan.registry_changes[0]
    if change not in grant.allowed_registry_changes:
        return _installation_failed(ctx, plan, "registry mutation is outside the InstallationGrant")
    if plan.strategy == ExtensionStrategy.REGISTER_EXISTING_PROCESS.value:
        original = ctx.services.get_definition(change["definition_name"], change["definition_version"])
        if original is None:
            return _installation_failed(ctx, plan, "existing ProcessDefinition disappeared before activation")
        definition = ProcessDefinition(
            name=original.name, version=original.version, handler=original.handler,
            trigger_event_types=original.trigger_event_types, max_retries=original.max_retries,
            metadata={**original.metadata, "installation_plan_id": str(plan.id), "active": True},
            context_requirements=original.context_requirements,
            provides_capabilities=tuple(plan.target_capabilities),
        )
    else:
        definition = ProcessDefinition(
            name=change["definition_name"], version=change["definition_version"],
            handler="installed_extension_handler", trigger_event_types=(), max_retries=0,
            metadata={
                "role": "installed_extension", "installation_plan_id": str(plan.id),
                "strategy": plan.strategy, "active": True,
            },
            provides_capabilities=tuple(plan.target_capabilities),
        )
    capabilities = [
        Capability(
            name=name, version=plan.component_version,
            metadata={
                "installation_plan_id": str(plan.id),
                "construction_result_id": str(plan.construction_result_id),
                "artifact_version_ids": [str(a.resource_version_id) for a in plan.artifact_versions],
                "artifact_hashes": [a.content_hash for a in plan.artifact_versions],
            },
        )
        for name in plan.target_capabilities
    ]
    activation = ActivationRecord(
        installation_plan_id=plan.id, component_name=plan.component_name,
        component_version=plan.component_version, strategy=plan.strategy,
        definition_name=definition.name, definition_version=definition.version,
        capabilities=list(plan.target_capabilities),
        artifact_versions=[a.resource_version_id for a in plan.artifact_versions],
        artifact_hashes=[a.content_hash for a in plan.artifact_versions],
        installed_root=str(manager.component_root(plan.component_name, plan.component_version)),
        previous_state=dict((plan.rollback_spec or {}).get("previous_state") or {}),
    )
    if plan.strategy != ExtensionStrategy.REGISTER_EXISTING_PROCESS.value:
        try:
            manager.smoke(activation)
        except (OSError, ImportError, SyntaxError, AttributeError, ValueError, TypeError) as exc:
            return _installation_failed(ctx, plan, f"activation load failed: {exc}")
    ctx.activate_installation(activation, definition, capabilities)
    ctx.update_installation_plan(plan.id, InstallationPlanStatus.ACTIVATED)
    ctx.update_installation_grant(grant.id, InstallationGrantStatus.REVOKED, revoked_at=utcnow())
    for step in plan.steps:
        if step.step_type in {
            InstallationStepType.REGISTER_COMPONENT,
            InstallationStepType.REGISTER_PROCESS_DEFINITION,
            InstallationStepType.REGISTER_EXTRACTOR,
            InstallationStepType.ENABLE_COMPONENT,
            InstallationStepType.SET_ACTIVE_VERSION,
        }:
            ctx.update_installation_step(step.id, InstallationStepStatus.COMPLETED)
    result.status = InstallationResultStatus.ACTIVATED
    result.activated_capabilities = list(plan.target_capabilities)
    result.evidence = {**result.evidence, "activation_record_id": str(activation.id)}
    ctx.record_installation_result(result)
    events = [
        ctx.new_event("capability_available", {
            "capability_name": capability.name,
            "capability_version": capability.version,
            "definition_name": definition.name,
            "definition_version": definition.version,
            "installation_plan_id": str(plan.id),
        })
        for capability in capabilities
    ]
    events.append(ctx.new_event(EXTENSION_ACTIVATED, _payload(plan, activation_record_id=str(activation.id))))
    return ctx.complete(output={"activated": True, "capabilities": plan.target_capabilities}, emitted_events=events)


async def rollback_installation(ctx: ProcessContext) -> ProcessResult:
    """Remove only the failed version; the previous active state is untouched."""
    if ctx.resume_point == "await_rollback_action":
        return _resume_rollback(ctx)
    assert ctx.event is not None and ctx.services is not None
    plan = ctx.services.get_installation_plan(_uuid(ctx.event.payload.get("installation_plan_id")))
    if plan is None:
        return ctx.fail("rollback plan not found")
    previous = ctx.services.get_installation_store().rollback_for_plan(plan.id)
    if previous is not None and previous.status is RollbackStatus.COMPLETED:
        return ctx.complete(output={"rolled_back": True, "reason": "already complete"})
    grant = ctx.services.get_installation_grant_for_plan(plan.id)
    if grant is None or grant.status is not InstallationGrantStatus.ACTIVE:
        return ctx.complete(output={"rolled_back": False, "reason": "InstallationGrant unavailable"})
    target = str((plan.rollback_spec or {}).get("new_installed_root", ""))
    ctx.update_installation_plan(plan.id, InstallationPlanStatus.ROLLING_BACK)
    ctx.record_rollback(RollbackRecord(
        installation_plan_id=plan.id, status=RollbackStatus.RUNNING,
        restored_state=dict((plan.rollback_spec or {}).get("previous_state") or {}),
        id=previous.id if previous else uuid.uuid4(), created_at=previous.created_at if previous else utcnow(),
    ))
    proposal = ctx.propose_action(
        backend=BACKEND_NAME, action_type="rollback_installation", target=target,
        parameters={"installation_plan_id": str(plan.id), "installation_grant_id": str(grant.id)},
        required_permissions=["production.install"], declared_side_effects=["filesystem_write"],
        risk_level=RiskLevel.LOW, rationale="remove only the failed reviewed installed version",
        idempotency_key=f"installation:{plan.id}:rollback",
    )
    return ctx.suspend(
        resume_point="await_rollback_action", waiting_for=waiting_for_action(proposal),
        saved_process_state={"installation_plan_id": str(plan.id), "grant_id": str(grant.id)},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


def _resume_rollback(ctx: ProcessContext) -> ProcessResult:
    assert ctx.event is not None and ctx.services is not None
    plan = ctx.services.get_installation_plan(_uuid(ctx.saved_process_state.get("installation_plan_id")))
    grant = ctx.services.get_installation_grant_for_plan(plan.id) if plan else None
    if plan is None or grant is None:
        return ctx.fail("rollback state disappeared")
    existing = ctx.services.get_installation_result(plan.id)
    if ctx.event.type != "action_succeeded":
        ctx.record_rollback(RollbackRecord(
            installation_plan_id=plan.id, status=RollbackStatus.FAILED,
            failure_reason=f"rollback action ended as {ctx.event.type}",
        ))
        return ctx.complete(output={"rolled_back": False})
    rollback = RollbackRecord(
        installation_plan_id=plan.id, status=RollbackStatus.COMPLETED,
        restored_state=dict((plan.rollback_spec or {}).get("previous_state") or {}),
        removed_destinations=list(plan.production_destinations), completed_at=utcnow(),
    )
    result = InstallationResult(
        installation_plan_id=plan.id, status=InstallationResultStatus.ROLLED_BACK,
        installed_artifact_versions=list(existing.installed_artifact_versions) if existing else [],
        previous_state=rollback.restored_state,
        failure_reason=existing.failure_reason if existing else None,
        id=existing.id if existing else uuid.uuid4(), created_at=existing.created_at if existing else utcnow(),
    )
    ctx.record_rollback(rollback)
    ctx.record_installation_result(result)
    ctx.update_installation_plan(plan.id, InstallationPlanStatus.ROLLED_BACK)
    ctx.update_installation_grant(grant.id, InstallationGrantStatus.REVOKED, revoked_at=utcnow())
    return ctx.complete(
        output={"rolled_back": True},
        emitted_events=[ctx.new_event(INSTALLATION_ROLLED_BACK, _payload(plan))],
    )


async def installed_extension_handler(ctx: ProcessContext) -> ProcessResult:
    """Generic Process role for a safely installed generated capability."""
    assert ctx.services is not None
    activation = ctx.services.get_activation_for_definition(
        ctx.instance.definition_name, ctx.instance.definition_version
    )
    if activation is None:
        return ctx.fail("installed capability has no ACTIVE activation record")
    try:
        function = ctx.services.get_installation_manager().capability_function(activation)
        output = function(dict(ctx.instance.input or {}))
    except (OSError, ImportError, SyntaxError, AttributeError, ValueError, TypeError) as exc:
        return ctx.fail(f"installed capability failed: {exc}")
    ctx.satisfy_work()
    return ctx.complete(
        output=output if isinstance(output, dict) else {"value": output},
        emitted_events=[ctx.new_event("work_satisfied", {
            "work_requirement_id": str(ctx.instance.work_requirement_id) if ctx.instance.work_requirement_id else None,
            "work_key": ctx.instance.work_key,
            "installation_plan_id": str(activation.installation_plan_id),
        })],
    )


async def resolve_activated_capability_gap(ctx: ProcessContext) -> ProcessResult:
    """Resolve the gap only after the original Work really reached SATISFIED."""
    assert ctx.event is not None and ctx.services is not None
    work_id = _uuid(ctx.event.payload.get("work_requirement_id"))
    requirement = ctx.services.get_work_requirement(work_id) if work_id else None
    if requirement is None or requirement.status is not WorkStatus.SATISFIED:
        return ctx.complete(output={"resolved": False})
    resolved = []
    for gap in ctx.services.get_extension_store().gaps_for_work(requirement.id):
        if gap.status is not CapabilityGapStatus.RESOLVED:
            ctx.update_capability_gap(gap.id, CapabilityGapStatus.RESOLVED)
            resolved.append(str(gap.id))
    return ctx.complete(output={"resolved": resolved})


PLAN_EXTENSION_INSTALLATION = ProcessDefinition(
    name="plan_extension_installation", version="1", handler="plan_extension_installation",
    trigger_event_types=(EXTENSION_CONSTRUCTION_VERIFIED,), metadata={"role": "installation_planner"},
)
INSTALL_EXTENSION = ProcessDefinition(
    name="install_extension", version="1", handler="install_extension",
    trigger_event_types=(INSTALLATION_APPROVED,), max_retries=2,
    metadata={"role": "installation_executor", "permissions": ["production.install"]},
)
VERIFY_INSTALLED_EXTENSION = ProcessDefinition(
    name="verify_installed_extension", version="1", handler="verify_installed_extension",
    trigger_event_types=(EXTENSION_INSTALLED,), metadata={"role": "installation_verifier"},
)
ACTIVATE_INSTALLED_EXTENSION = ProcessDefinition(
    name="activate_installed_extension", version="1", handler="activate_installed_extension",
    trigger_event_types=(INSTALLATION_VERIFIED,), metadata={"role": "capability_activator"},
)
ROLLBACK_INSTALLATION = ProcessDefinition(
    name="rollback_installation", version="1", handler="rollback_installation",
    trigger_event_types=(INSTALLATION_ROLLBACK_REQUESTED,), max_retries=2,
    metadata={"role": "installation_rollback", "permissions": ["production.install"]},
)
RESOLVE_ACTIVATED_CAPABILITY_GAP = ProcessDefinition(
    name="resolve_activated_capability_gap", version="1", handler="resolve_activated_capability_gap",
    trigger_event_types=("work_satisfied",), metadata={"role": "capability_gap_reconciler"},
)


def bootstrap_installation(runtime) -> None:
    """Register the Phase 5C chain and reconstruct in-memory ACTIVE components."""
    bootstrap_construction(runtime)
    bootstrap_actions(runtime)
    runtime.register_process(PLAN_EXTENSION_INSTALLATION, plan_extension_installation)
    runtime.register_process(INSTALL_EXTENSION, install_extension)
    runtime.register_process(VERIFY_INSTALLED_EXTENSION, verify_installed_extension)
    runtime.register_process(ACTIVATE_INSTALLED_EXTENSION, activate_installed_extension)
    runtime.register_process(ROLLBACK_INSTALLATION, rollback_installation)
    runtime.register_process(RESOLVE_ACTIVATED_CAPABILITY_GAP, resolve_activated_capability_gap)
    runtime.registry.register("installed_extension_handler", installed_extension_handler)
    runtime.installation_manager.restore_active(runtime.installation_store, runtime.extractors)


__all__ = [
    "ACTIVATE_INSTALLED_EXTENSION", "EXTENSION_ACTIVATED", "EXTENSION_INSTALLED",
    "INSTALLATION_APPROVED", "INSTALLATION_REVIEWED", "INSTALLATION_REVIEW_REQUIRED",
    "INSTALLATION_ROLLED_BACK", "INSTALLATION_ROLLBACK_REQUESTED", "INSTALLATION_VERIFIED",
    "INSTALL_EXTENSION", "PLAN_EXTENSION_INSTALLATION", "ROLLBACK_INSTALLATION",
    "VERIFY_INSTALLED_EXTENSION", "activate_installed_extension", "bootstrap_installation",
    "install_extension", "installed_extension_handler", "plan_extension_installation",
    "rollback_installation", "verify_installed_extension",
]
