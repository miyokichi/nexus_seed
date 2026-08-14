"""Phase 5B as ordinary durable Processes, stopping at VERIFIED."""

from __future__ import annotations

import uuid
from pathlib import Path

from ..construction.generator import (
    deterministic_artifacts,
    validate_generated_artifacts,
)
from ..construction.models import (
    CapabilityContract,
    ConstructionGrant,
    ConstructionGrantStatus,
    ConstructionPlanStatus,
    ConstructionResult,
    ConstructionResultStatus,
    ConstructionStepStatus,
    SandboxWorkspace,
    SandboxWorkspaceStatus,
    VerificationCheck,
    VerificationLayer,
    VerificationStatus,
)
from ..construction.runner import StructuredTestRunner
from ..construction.validator import sandbox_permissions_for
from ..construction.workspace import BACKEND_NAME
from ..context.requirements import ContextRequirements, ContinuationReq
from ..core.event import utcnow
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..extension.models import (
    CapabilityGapStatus,
    ExtensionProposalStatus,
)
from ..resources.models import ResourceRepresentation, text_hash
from ..work.work_requirement import WorkStatus
from .actions import action_proposed_event, bootstrap_actions, waiting_for_action


EXTENSION_CONSTRUCTION_READY = "extension_construction_ready"
EXTENSION_CONSTRUCTION_EXECUTED = "extension_construction_executed"
EXTENSION_CONSTRUCTION_VERIFIED = "extension_construction_verified"
EXTENSION_CONSTRUCTION_FAILED = "extension_construction_failed"
EXTENSION_CONSTRUCTION_RETRY_REQUESTED = "extension_construction_retry_requested"
WORK_CANCELLED = "work_cancelled"

PLAN_EXTENSION_CONSTRUCTION = ProcessDefinition(
    name="plan_extension_construction", version="1",
    handler="plan_extension_construction",
    trigger_event_types=("extension_approved", EXTENSION_CONSTRUCTION_RETRY_REQUESTED),
    context_requirements=ContextRequirements(include_trigger_event=True),
    metadata={"role": "construction_planner"},
)

EXECUTE_EXTENSION_CONSTRUCTION = ProcessDefinition(
    name="execute_extension_construction", version="1",
    handler="execute_extension_construction",
    trigger_event_types=(EXTENSION_CONSTRUCTION_READY,), max_retries=2,
    context_requirements=ContextRequirements(
        include_trigger_event=True, continuation=ContinuationReq(include=True)
    ),
    metadata={
        "role": "construction_executor",
        # This fixed definition authority is not a global permission mutation.
        # The backend additionally requires the per-plan ConstructionGrant.
        "permissions": ["sandbox.read", "sandbox.write", "sandbox.test"],
    },
)

VERIFY_EXTENSION_CONSTRUCTION = ProcessDefinition(
    name="verify_extension_construction", version="1",
    handler="verify_extension_construction",
    trigger_event_types=(EXTENSION_CONSTRUCTION_EXECUTED,),
    context_requirements=ContextRequirements(include_trigger_event=True),
    metadata={"role": "construction_verifier"},
)

CLEANUP_EXTENSION_LIFECYCLE = ProcessDefinition(
    name="cleanup_extension_lifecycle", version="1",
    handler="cleanup_extension_lifecycle",
    trigger_event_types=(WORK_CANCELLED,),
    context_requirements=ContextRequirements(include_trigger_event=True),
    metadata={"role": "extension_lifecycle_cleanup"},
)


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def _payload(plan, **extra) -> dict:
    value = {
        "construction_plan_id": str(plan.id),
        "extension_proposal_id": str(plan.extension_proposal_id),
        "capability_gap_id": str(plan.capability_gap_id),
        "work_requirement_id": str(plan.work_requirement_id),
        "target_capabilities": list(plan.target_capabilities),
    }
    value.update(extra)
    return value


async def plan_extension_construction(ctx: ProcessContext) -> ProcessResult:
    """Create one logical validated plan, workspace record and scoped grant."""
    assert ctx.event is not None and ctx.services is not None
    proposal_id = _uuid(ctx.event.payload.get("extension_proposal_id")) or _uuid(
        ctx.event.payload.get("proposal_id")
    )
    proposal = ctx.services.get_extension_proposal(proposal_id)
    if proposal is None:
        return ctx.fail(f"extension proposal {proposal_id} not found")
    if proposal.status is not ExtensionProposalStatus.APPROVED:
        return ctx.complete(output={"planned": False, "reason": f"proposal is {proposal.status.value}"})
    requirement = ctx.services.get_work_requirement(proposal.work_requirement_id)
    if requirement is None or requirement.status is WorkStatus.CANCELLED:
        return ctx.complete(output={"planned": False, "reason": "work is cancelled or missing"})
    existing_plans = ctx.services.get_construction_plans_for_proposal(proposal.id)
    requested_attempt = int(ctx.event.payload.get("construction_attempt") or 1)
    if existing_plans and requested_attempt <= max(plan.attempt for plan in existing_plans):
        existing = existing_plans[-1]
        return ctx.complete(
            output={"planned": False, "construction_plan_id": str(existing.id), "reason": "proposal was already planned"}
        )
    gap = ctx.services.get_capability_gap(proposal.capability_gap_id)
    if gap is None or gap.status is CapabilityGapStatus.CANCELLED:
        return ctx.complete(output={"planned": False, "reason": "capability gap is cancelled or missing"})

    planner = ctx.services.get_construction_planner()
    if requested_attempt > 1:
        previous = max(existing_plans, key=lambda item: item.attempt) if existing_plans else None
        retry_after_installation = (
            ctx.event.payload.get("retry_reason_stage") == "INSTALLATION"
        )
        if (
            previous is None
            or not previous.status.terminal
            or (
                previous.status is ConstructionPlanStatus.VERIFIED
                and not retry_after_installation
            )
        ):
            return ctx.complete(output={"planned": False, "reason": "previous construction attempt is not retryable"})
        if requested_attempt != previous.attempt + 1:
            return ctx.complete(output={"planned": False, "reason": "construction attempt is not consecutive"})
    plan = planner.build(
        proposal, gap, context_snapshot_id=ctx.context_snapshot_id,
        attempt=requested_attempt,
    )
    validation = ctx.services.get_construction_validator().validate(plan, proposal)
    if not validation.ok:
        plan.status = ConstructionPlanStatus.FAILED
        ctx.record_construction_plan(plan)
        result = ConstructionResult(
            construction_plan_id=plan.id, extension_proposal_id=proposal.id,
            status=ConstructionResultStatus.FAILED,
            failure_reason="; ".join(validation.reasons),
            evidence={"validation_reasons": validation.reasons},
        )
        ctx.record_construction_result(result)
        return ctx.complete(
            output={"planned": False, "reasons": validation.reasons},
            emitted_events=[ctx.new_event(EXTENSION_CONSTRUCTION_FAILED, _payload(plan, reasons=validation.reasons))],
        )

    plan.status = ConstructionPlanStatus.READY
    limits = plan.sandbox_requirements
    workspace = SandboxWorkspace(
        construction_plan_id=plan.id, root_locator="",
        max_files=int(limits.get("max_files", 32)),
        max_file_bytes=int(limits.get("max_file_bytes", 256_000)),
        max_total_bytes=int(limits.get("max_total_bytes", 1_000_000)),
        timeout_seconds=float(limits.get("timeout_seconds", 10.0)),
    )
    workspace.root_locator = ctx.services.get_workspace_manager().make_locator(workspace.id)
    grant = ConstructionGrant(
        construction_plan_id=plan.id, workspace_id=workspace.id,
        allowed_permissions=sandbox_permissions_for(proposal.required_permissions),
        allowed_root=workspace.root_locator,
    )
    ctx.record_construction_plan(plan)
    ctx.record_sandbox_workspace(workspace)
    ctx.record_construction_grant(grant)
    return ctx.complete(
        output={"planned": True, "construction_plan_id": str(plan.id)},
        emitted_events=[ctx.new_event(
            EXTENSION_CONSTRUCTION_READY,
            _payload(
                plan, workspace_id=str(workspace.id),
                construction_attempt=plan.attempt,
            ),
        )],
    )


async def execute_extension_construction(ctx: ProcessContext) -> ProcessResult:
    """Generate artifact text, then write each file through ActionProposal."""
    if ctx.resume_point == "await_construction_action":
        return _resume_construction_action(ctx)
    assert ctx.event is not None and ctx.services is not None
    plan_id = _uuid(ctx.event.payload.get("construction_plan_id"))
    plan = ctx.services.get_construction_plan(plan_id)
    if plan is None:
        return ctx.fail(f"construction plan {plan_id} not found")
    if plan.status not in {ConstructionPlanStatus.READY, ConstructionPlanStatus.RUNNING}:
        return ctx.complete(output={"executed": False, "reason": f"plan is {plan.status.value}"})
    requirement = ctx.services.get_work_requirement(plan.work_requirement_id)
    if requirement is None or requirement.status is WorkStatus.CANCELLED:
        return _cancel_plan(ctx, plan, "work was cancelled before construction")
    proposal = ctx.services.get_extension_proposal(plan.extension_proposal_id)
    workspace = ctx.services.get_sandbox_workspace_for_plan(plan.id)
    grant = ctx.services.get_construction_grant_for_plan(plan.id)
    if proposal is None or workspace is None or grant is None:
        return ctx.fail("construction plan dependencies are missing")
    try:
        ctx.services.get_workspace_manager().ensure(workspace)
    except OSError as exc:
        if ctx.instance.retry_count < ctx.instance.max_retries:
            return ctx.retry(exc)
        return _fail_plan(ctx, plan, workspace, grant, str(exc))

    generator = ctx.services.get_llm_construction_generator()
    if generator is None:
        artifacts = deterministic_artifacts(plan, proposal)
    else:
        raw, invocation = await generator.generate(
            plan, proposal, process_instance_id=ctx.instance.id,
            context=ctx.view.to_snapshot_dict() if ctx.view else {},
        )
        invocation.context_snapshot_id = ctx.context_snapshot_id
        ctx.record_llm_invocation(invocation)
        if not invocation.success:
            error = invocation.error or "construction generator failed"
            if ctx.instance.retry_count < ctx.instance.max_retries:
                return ctx.retry(error)
            return _fail_plan(ctx, plan, workspace, grant, error)
        artifacts, reasons = validate_generated_artifacts(
            raw, max_files=workspace.max_files,
            max_file_bytes=workspace.max_file_bytes,
            max_total_bytes=workspace.max_total_bytes,
        )
        if reasons:
            return _fail_plan(ctx, plan, workspace, grant, "; ".join(reasons))
        plan.llm_invocation_id = invocation.id
        ctx.record_construction_plan(plan)
    # Apply the same validation to deterministic output, and require the
    # declared artifact set so a model cannot silently omit tests/fixtures.
    artifact_dict = {a.relative_path: a for a in artifacts}
    missing = [a.relative_path for a in plan.expected_artifacts if a.required and a.relative_path not in artifact_dict]
    unexpected = [p for p in artifact_dict if p not in {a.relative_path for a in plan.expected_artifacts}]
    if missing or unexpected:
        return _fail_plan(ctx, plan, workspace, grant, f"artifact set mismatch; missing={missing}, unexpected={unexpected}")
    serialized = [
        {"relative_path": a.relative_path, "content": a.content, "artifact_role": a.artifact_role}
        for a in artifacts
    ]
    ctx.update_construction_plan(plan.id, ConstructionPlanStatus.RUNNING)
    ctx.update_construction_step(plan.steps[0].id, ConstructionStepStatus.RUNNING)
    ctx.update_sandbox_workspace(workspace.id, SandboxWorkspaceStatus.ACTIVE)
    return _propose_artifact_write(ctx, plan, workspace, grant, serialized, 0, [])


def _propose_artifact_write(ctx, plan, workspace, grant, artifacts, index, resource_ids):
    artifact = artifacts[index]
    relative = artifact["relative_path"]
    proposal = ctx.propose_action(
        backend=BACKEND_NAME, action_type="write_artifact",
        target=f"{workspace.id}/{relative}".replace("\\", "/"),
        parameters={
            **artifact, "construction_plan_id": str(plan.id),
            "workspace_id": str(workspace.id),
        },
        required_permissions=["sandbox.write"],
        declared_side_effects=["sandbox_write"], risk_level="LOW",
        rationale="materialize one validated construction artifact inside its workspace",
        idempotency_key=f"construction:{plan.id}:step:0:{relative}",
    )
    return ctx.suspend(
        resume_point="await_construction_action",
        waiting_for=waiting_for_action(proposal),
        saved_process_state={
            "construction_plan_id": str(plan.id), "workspace_id": str(workspace.id),
            "grant_id": str(grant.id), "artifacts": artifacts,
            "artifact_index": index, "artifact_resource_ids": resource_ids,
            "action_proposal_id": str(proposal.id),
        },
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


def _resume_construction_action(ctx: ProcessContext) -> ProcessResult:
    assert ctx.event is not None and ctx.services is not None
    state = ctx.saved_process_state
    plan = ctx.services.get_construction_plan(_uuid(state.get("construction_plan_id")))
    workspace = ctx.services.get_sandbox_workspace_for_plan(plan.id) if plan else None
    grant = ctx.services.get_construction_grant_for_plan(plan.id) if plan else None
    if plan is None or workspace is None or grant is None:
        return ctx.fail("construction state disappeared while awaiting an action")
    if ctx.event.type != "action_succeeded":
        return _fail_plan(ctx, plan, workspace, grant, f"artifact write ended as {ctx.event.type}")
    artifacts = list(state.get("artifacts") or [])
    index = int(state.get("artifact_index", 0))
    artifact = artifacts[index]
    resource_ids = list(state.get("artifact_resource_ids") or [])
    resource_id = _stage_generated_resource(ctx, plan, workspace, artifact)
    if str(resource_id) not in resource_ids:
        resource_ids.append(str(resource_id))
    next_index = index + 1
    if next_index < len(artifacts):
        return _propose_artifact_write(
            ctx, plan, workspace, grant, artifacts, next_index, resource_ids
        )
    ctx.update_construction_step(plan.steps[0].id, ConstructionStepStatus.COMPLETED)
    ctx.update_construction_plan(plan.id, ConstructionPlanStatus.VERIFYING)
    return ctx.complete(
        output={"executed": True, "artifacts": len(artifacts)},
        emitted_events=[ctx.new_event(
            EXTENSION_CONSTRUCTION_EXECUTED,
            _payload(plan, workspace_id=str(workspace.id), artifact_resource_ids=resource_ids),
        )],
    )


def _stage_generated_resource(ctx, plan, workspace, artifact) -> uuid.UUID:
    relative = artifact["relative_path"]
    content = artifact["content"]
    metadata = {
        "construction_plan_id": str(plan.id),
        "extension_proposal_id": str(plan.extension_proposal_id),
        "workspace_id": str(workspace.id),
        "generated_by_process_id": str(ctx.instance.id),
        "artifact_role": artifact["artifact_role"],
    }
    service = ctx.services.get_resource_service()
    indexed = service.index_observation(
        uri=f"construction://{workspace.id}/{relative}",
        locator=str(Path(workspace.root_locator) / relative),
        observed_hash=text_hash(content), size_bytes=len(content.encode("utf-8")),
        resource_type="python" if relative.endswith(".py") else "text",
        source_identity=f"{workspace.id}:{relative}", metadata=metadata,
    )
    resource = indexed.resource
    if indexed.resource_is_new:
        ctx.add_resource(resource)
    if indexed.version is not None:
        ctx.add_resource_version(indexed.version)
        ctx.add_representation(ResourceRepresentation(
            resource_version_id=indexed.version.id,
            representation_type="generated_source", content=content,
            extractor_name="construction", extractor_version="1",
            metadata=metadata, created_by_process_id=ctx.instance.id,
        ))
    return resource.id


async def verify_extension_construction(ctx: ProcessContext) -> ProcessResult:
    """Run structural, static and behavior layers; tests alone never suffice."""
    assert ctx.event is not None and ctx.services is not None
    plan = ctx.services.get_construction_plan(_uuid(ctx.event.payload.get("construction_plan_id")))
    if plan is None:
        return ctx.fail("construction plan not found for verification")
    existing = ctx.services.get_construction_result(plan.id)
    if existing is not None:
        return ctx.complete(output={"verified": existing.status is ConstructionResultStatus.VERIFIED, "reason": "already decided"})
    workspace = ctx.services.get_sandbox_workspace_for_plan(plan.id)
    grant = ctx.services.get_construction_grant_for_plan(plan.id)
    if workspace is None or grant is None:
        return ctx.fail("workspace or grant missing for verification")
    runner = StructuredTestRunner(workspace.root_locator, timeout_seconds=workspace.timeout_seconds)
    structural = runner.structural(plan.expected_artifacts)
    static = runner.static([a.relative_path for a in plan.expected_artifacts])
    sandbox_tests = (
        runner.python_tests([
            a.relative_path for a in plan.expected_artifacts
            if a.artifact_role == "test"
        ])
        if structural.passed and static.passed
        else None
    )
    if sandbox_tests is not None:
        from ..construction.runner import TestRunResult, TestRunType
        static = TestRunResult(
            TestRunType.PYTHON_IMPORT,
            passed=static.passed and sandbox_tests.passed,
            blocked=static.blocked or sandbox_tests.blocked,
            evidence={
                "static_import": static.evidence,
                "sandbox_tests": sandbox_tests.evidence,
            },
            error="; ".join(
                e for e in (static.error, sandbox_tests.error) if e
            ) or None,
        )
    behavior_requirement = next(
        (r for r in plan.verification_requirements if str(r.get("layer", "")).upper() == "BEHAVIOR"), {}
    )
    contract = CapabilityContract.from_dict(behavior_requirement.get("contract") or {})
    implementation = next(
        (a.relative_path for a in plan.expected_artifacts if a.artifact_role == "implementation" and a.relative_path.endswith(".py")), None
    )
    behavior = runner.behavior(contract, implementation) if structural.passed and static.passed else None
    triples = [
        (VerificationLayer.STRUCTURAL, "STRUCTURAL", structural),
        (VerificationLayer.STATIC, "PYTHON_STATIC_IMPORT", static),
        (VerificationLayer.BEHAVIOR, "CAPABILITY_CONTRACT", behavior),
    ]
    checks = []
    for layer, check_type, run in triples:
        status = (
            VerificationStatus.BLOCKED
            if run is None and static.blocked
            else VerificationStatus.FAIL
            if run is None
            else
            VerificationStatus.BLOCKED if run.blocked else VerificationStatus.PASS if run.passed else VerificationStatus.FAIL
        )
        check = VerificationCheck(
            construction_plan_id=plan.id, layer=layer, check_type=check_type,
            check_key=layer.value.lower(), status=status,
            evidence=run.evidence if run else {}, failure_reason=run.error if run else "prerequisite failed",
            completed_at=utcnow(),
            id=uuid.uuid5(plan.id, f"verification:{layer.value}"),
        )
        ctx.record_verification_check(check)
        checks.append(check)
    blocked = any(c.status is VerificationStatus.BLOCKED for c in checks)
    passed = all(c.status is VerificationStatus.PASS for c in checks)
    status = ConstructionResultStatus.VERIFIED if passed else ConstructionResultStatus.BLOCKED if blocked else ConstructionResultStatus.FAILED
    failure = None if passed else "; ".join(c.failure_reason or c.status.value for c in checks if c.status is not VerificationStatus.PASS)
    resource_ids = [_uuid(v) for v in ctx.event.payload.get("artifact_resource_ids", [])]
    result = ConstructionResult(
        construction_plan_id=plan.id, extension_proposal_id=plan.extension_proposal_id,
        status=status, artifact_resource_ids=[v for v in resource_ids if v],
        verification_check_ids=[c.id for c in checks],
        provided_capabilities=list(plan.target_capabilities) if passed else [],
        evidence={"layers": {c.layer.value: c.status.value for c in checks}},
        failure_reason=failure,
    )
    ctx.record_construction_result(result)
    ctx.update_construction_step(plan.steps[1].id, ConstructionStepStatus.COMPLETED if static.passed else ConstructionStepStatus.BLOCKED if static.blocked else ConstructionStepStatus.FAILED)
    ctx.update_construction_step(
        plan.steps[2].id,
        ConstructionStepStatus.COMPLETED
        if behavior and behavior.passed
        else ConstructionStepStatus.BLOCKED
        if static.blocked
        else ConstructionStepStatus.FAILED,
    )
    ctx.update_construction_plan(plan.id, ConstructionPlanStatus.VERIFIED if passed else ConstructionPlanStatus.BLOCKED if blocked else ConstructionPlanStatus.FAILED)
    ctx.update_sandbox_workspace(workspace.id, SandboxWorkspaceStatus.SEALED if passed else SandboxWorkspaceStatus.FAILED, closed_at=utcnow())
    ctx.update_construction_grant(grant.id, ConstructionGrantStatus.REVOKED)
    event_type = EXTENSION_CONSTRUCTION_VERIFIED if passed else EXTENSION_CONSTRUCTION_FAILED
    return ctx.complete(
        output={"verified": passed, "status": status.value, "construction_result_id": str(result.id)},
        emitted_events=[ctx.new_event(event_type, _payload(
            plan, construction_result_id=str(result.id), status=status.value,
            provided_capabilities=result.provided_capabilities, failure_reason=failure,
        ))],
    )


async def cleanup_extension_lifecycle(ctx: ProcessContext) -> ProcessResult:
    """Close a cancelled need's gaps, live proposals and review waiters."""
    assert ctx.event is not None and ctx.services is not None
    work_id = _uuid(ctx.event.payload.get("work_requirement_id"))
    if work_id is None:
        return ctx.complete(output={"cleaned": False, "reason": "work id missing"})
    store = ctx.services.get_extension_store()
    cancelled_proposals = 0
    for gap in store.gaps_for_work(work_id):
        if not gap.status.terminal:
            ctx.update_capability_gap(gap.id, CapabilityGapStatus.CANCELLED)
        for proposal in store.proposals_for_gap(gap.id):
            if proposal.status.live:
                ctx.update_extension_proposal(
                    proposal.id, ExtensionProposalStatus.CANCELLED,
                    reasons=["source work was cancelled"],
                )
                cancelled_proposals += 1
                for continuation in ctx.services.find_extension_review_continuations(proposal.id):
                    ctx.close_continuation(
                        continuation.id,
                        process_instance_id=continuation.process_instance_id,
                        reason="source work cancelled",
                    )
            plan = ctx.services.get_active_construction_plan(proposal.id)
            if plan is not None:
                ctx.update_construction_plan(plan.id, ConstructionPlanStatus.CANCELLED)
    return ctx.complete(output={"cleaned": True, "cancelled_proposals": cancelled_proposals})


def _fail_plan(ctx, plan, workspace, grant, reason) -> ProcessResult:
    ctx.update_construction_plan(plan.id, ConstructionPlanStatus.FAILED)
    if plan.steps and plan.steps[0].status is not ConstructionStepStatus.COMPLETED:
        ctx.update_construction_step(plan.steps[0].id, ConstructionStepStatus.FAILED)
    ctx.update_sandbox_workspace(workspace.id, SandboxWorkspaceStatus.FAILED, closed_at=utcnow())
    ctx.update_construction_grant(grant.id, ConstructionGrantStatus.REVOKED)
    result = ConstructionResult(
        construction_plan_id=plan.id, extension_proposal_id=plan.extension_proposal_id,
        status=ConstructionResultStatus.FAILED, failure_reason=reason,
    )
    ctx.record_construction_result(result)
    return ctx.complete(
        output={"executed": False, "reason": reason},
        emitted_events=[ctx.new_event(EXTENSION_CONSTRUCTION_FAILED, _payload(plan, failure_reason=reason))],
    )


def _cancel_plan(ctx, plan, reason) -> ProcessResult:
    ctx.update_construction_plan(plan.id, ConstructionPlanStatus.CANCELLED)
    return ctx.complete(output={"executed": False, "reason": reason})


def bootstrap_construction(runtime) -> None:
    """Register construction, its reused Action boundary, and no activator."""
    bootstrap_actions(runtime)
    runtime.register_process(PLAN_EXTENSION_CONSTRUCTION, plan_extension_construction)
    runtime.register_process(EXECUTE_EXTENSION_CONSTRUCTION, execute_extension_construction)
    runtime.register_process(VERIFY_EXTENSION_CONSTRUCTION, verify_extension_construction)
    runtime.register_process(CLEANUP_EXTENSION_LIFECYCLE, cleanup_extension_lifecycle)


__all__ = [
    "CLEANUP_EXTENSION_LIFECYCLE", "EXECUTE_EXTENSION_CONSTRUCTION",
    "EXTENSION_CONSTRUCTION_EXECUTED", "EXTENSION_CONSTRUCTION_FAILED",
    "EXTENSION_CONSTRUCTION_READY", "EXTENSION_CONSTRUCTION_VERIFIED",
    "PLAN_EXTENSION_CONSTRUCTION", "VERIFY_EXTENSION_CONSTRUCTION",
    "bootstrap_construction", "cleanup_extension_lifecycle",
    "execute_extension_construction", "plan_extension_construction",
    "verify_extension_construction",
]
