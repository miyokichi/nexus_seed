"""Phase 5D bounded coordinator over the existing 5A/5B/5C processes.

The handler makes policy decisions and emits the same review events a human
would.  It never constructs, installs, registers, grants, or calls a backend.
"""

from __future__ import annotations

import uuid
from datetime import timezone

from ..capabilities.models import CapabilityRequirement
from ..autonomy.models import (
    AcquisitionAttempt, AcquisitionStage, AcquisitionStatus, AcquisitionSubscriber,
    ApprovalSource, AutonomyDecision, AutonomyDecisionKind,
    CapabilityAcquisitionSession, acquisition_key_for,
)
from ..construction.models import ConstructionResultStatus
from ..core.event import utcnow
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..extension.models import ExtensionProposalStatus
from .construction import (
    EXTENSION_CONSTRUCTION_FAILED, EXTENSION_CONSTRUCTION_READY,
    EXTENSION_CONSTRUCTION_VERIFIED,
)
from .extension import (
    CAPABILITY_GAP_OPENED, EXTENSION_APPROVED, EXTENSION_REVIEW_REQUIRED,
    EXTENSION_REVIEWED,
)
from .installation import (
    EXTENSION_ACTIVATED, EXTENSION_INSTALLED, INSTALLATION_APPROVED,
    INSTALLATION_REJECTED, INSTALLATION_REVIEWED, INSTALLATION_REVIEW_REQUIRED,
    INSTALLATION_ROLLED_BACK, INSTALLATION_VERIFIED,
)


AUTONOMY_REVIEWED = "autonomy_reviewed"
ACQUISITION_OPENED = "capability_acquisition_opened"
ACQUISITION_BLOCKED = "capability_acquisition_blocked"
ACQUISITION_COMPLETED = "capability_acquisition_completed"
CONSTRUCTION_RETRY_REQUESTED = "extension_construction_retry_requested"
ACQUISITION_DEPENDENCY_SATISFIED = "acquisition_dependency_satisfied"

ADVANCE_CAPABILITY_ACQUISITION = ProcessDefinition(
    name="advance_capability_acquisition",
    version="1",
    handler="advance_capability_acquisition",
    trigger_event_types=(
        EXTENSION_REVIEW_REQUIRED,
        CAPABILITY_GAP_OPENED,
        EXTENSION_APPROVED,
        EXTENSION_CONSTRUCTION_READY,
        EXTENSION_CONSTRUCTION_VERIFIED,
        EXTENSION_CONSTRUCTION_FAILED,
        INSTALLATION_REVIEW_REQUIRED,
        INSTALLATION_APPROVED,
        EXTENSION_INSTALLED,
        INSTALLATION_VERIFIED,
        INSTALLATION_REJECTED,
        INSTALLATION_ROLLED_BACK,
        EXTENSION_ACTIVATED,
        "capability_available",
        "work_satisfied",
        "work_cancelled",
        ACQUISITION_DEPENDENCY_SATISFIED,
        ACQUISITION_BLOCKED,
    ),
    metadata={"role": "capability_acquisition_orchestrator"},
)


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


async def advance_capability_acquisition(ctx: ProcessContext) -> ProcessResult:
    """Advance at most one durable boundary in a bounded acquisition."""

    if ctx.resume_point == "await_autonomy_review":
        return _resume_review(ctx)
    assert ctx.event is not None and ctx.services is not None
    event_type = ctx.event.type
    if event_type == EXTENSION_REVIEW_REQUIRED:
        return _open_or_join(ctx)
    if event_type == CAPABILITY_GAP_OPENED:
        return _gap_opened(ctx)
    if event_type in {"work_satisfied", "work_cancelled"}:
        return _work_finished(ctx)
    if event_type == "capability_available":
        return _capability_became_available(ctx)
    if event_type == ACQUISITION_DEPENDENCY_SATISFIED:
        return _dependency_satisfied(ctx)
    if event_type == ACQUISITION_BLOCKED:
        child = ctx.services.get_acquisition_session(
            _uuid(ctx.event.payload.get("acquisition_session_id"))
        )
        parent = (
            ctx.services.get_acquisition_session(child.parent_session_id)
            if child and child.parent_session_id else None
        )
        if parent and not parent.status.terminal:
            return _block(ctx, parent, f"DEPENDENCY_BLOCKED:{child.blocked_reason}")
        return ctx.complete(output={"advanced": False})

    session = _session_for_event(ctx)
    if session is None:
        # 5A may have been configured to approve without review.  Open the
        # coordinator from that approved proposal, while honoring the fact
        # that 5A already crossed its own validator/policy boundary.
        if event_type == EXTENSION_APPROVED:
            return _open_from_approved(ctx)
        return ctx.complete(output={"advanced": False, "reason": "no acquisition session"})
    if session.status.terminal:
        return ctx.complete(output={"advanced": False, "reason": f"session is {session.status.value}"})

    if event_type == EXTENSION_APPROVED:
        return _advance(session, ctx, AcquisitionStatus.CONSTRUCTING, AcquisitionStage.CONSTRUCTION)
    if event_type == EXTENSION_CONSTRUCTION_READY:
        plan_id = _uuid(ctx.event.payload.get("construction_plan_id"))
        attempt_number = int(ctx.event.payload.get("construction_attempt") or session.construction_attempts or 1)
        session.construction_plan_id = plan_id
        session.construction_attempts = max(session.construction_attempts, attempt_number)
        session.status = AcquisitionStatus.CONSTRUCTING
        session.current_stage = AcquisitionStage.CONSTRUCTION
        ctx.record_acquisition_session(session)
        ctx.record_acquisition_attempt(AcquisitionAttempt(
            acquisition_session_id=session.id, attempt_type="CONSTRUCTION",
            attempt_number=attempt_number, plan_id=plan_id,
            id=uuid.uuid5(session.id, f"construction:{attempt_number}"),
        ))
        return ctx.complete(output={"advanced": True, "construction_attempt": attempt_number})
    if event_type == EXTENSION_CONSTRUCTION_VERIFIED:
        result_id = _uuid(ctx.event.payload.get("construction_result_id"))
        session.construction_plan_id = _uuid(ctx.event.payload.get("construction_plan_id"))
        session.construction_result_id = result_id
        session.status = AcquisitionStatus.VERIFYING
        session.current_stage = AcquisitionStage.INSTALLATION
        ctx.record_acquisition_session(session)
        ctx.record_acquisition_attempt(AcquisitionAttempt(
            acquisition_session_id=session.id, attempt_type="CONSTRUCTION",
            attempt_number=max(1, session.construction_attempts),
            plan_id=session.construction_plan_id, result_id=result_id, status="VERIFIED",
            completed_at=utcnow(),
            id=uuid.uuid5(session.id, f"construction:{max(1, session.construction_attempts)}"),
        ))
        return ctx.complete(output={"advanced": True, "verified": True})
    if event_type == EXTENSION_CONSTRUCTION_FAILED:
        return _construction_failed(ctx, session)
    if event_type == INSTALLATION_REVIEW_REQUIRED:
        return _installation_review(ctx, session)
    if event_type == INSTALLATION_APPROVED:
        return _advance(session, ctx, AcquisitionStatus.INSTALLING, AcquisitionStage.INSTALLATION)
    if event_type == EXTENSION_INSTALLED:
        return _advance(session, ctx, AcquisitionStatus.VERIFYING, AcquisitionStage.INSTALLATION)
    if event_type == INSTALLATION_VERIFIED:
        return _advance(session, ctx, AcquisitionStatus.ACTIVATING, AcquisitionStage.ACTIVATION)
    if event_type == INSTALLATION_REJECTED:
        return _block(ctx, session, "INSTALLATION_REJECTED")
    if event_type == INSTALLATION_ROLLED_BACK:
        return _installation_rolled_back(ctx, session)
    if event_type == EXTENSION_ACTIVATED:
        session.status = AcquisitionStatus.COMPLETED
        session.current_stage = AcquisitionStage.COMPLETED
        session.completed_at = utcnow()
        session.blocked_reason = None
        ctx.record_acquisition_session(session)
        return ctx.complete(
            output={"advanced": True, "completed": True},
            emitted_events=[ctx.new_event(ACQUISITION_COMPLETED, _session_payload(session))],
        )
    return ctx.complete(output={"advanced": False, "reason": f"ignored {event_type}"})


def _open_or_join(ctx: ProcessContext) -> ProcessResult:
    proposal_id = _uuid(ctx.event.payload.get("extension_proposal_id")) or _uuid(ctx.event.payload.get("proposal_id"))
    proposal = ctx.services.get_extension_proposal(proposal_id)
    if proposal is None:
        return ctx.fail("extension proposal missing at autonomy boundary")
    gap = ctx.services.get_capability_gap(proposal.capability_gap_id)
    work = ctx.services.get_work_requirement(proposal.work_requirement_id)
    if gap is None or work is None:
        return ctx.fail("gap/work provenance missing at autonomy boundary")
    requirements = gap.missing_capabilities or gap.required_capabilities
    key = acquisition_key_for(requirements)
    store = ctx.services.get_autonomy_store()
    existing = store.find_by_key(key)
    subscriber = AcquisitionSubscriber(
        acquisition_session_id=existing.id if existing else uuid.uuid4(),
        work_requirement_id=work.id,
    )
    if existing is not None:
        subscriber.acquisition_session_id = existing.id
        subscriber.id = uuid.uuid5(existing.id, f"subscriber:{work.id}")
        ctx.record_acquisition_subscriber(subscriber)
        parent_id = _uuid(work.metadata.get("parent_acquisition_session_id"))
        ancestor = store.get_session(parent_id) if parent_id else None
        visited: set[uuid.UUID] = set()
        while ancestor is not None and ancestor.id not in visited:
            if ancestor.id == existing.id:
                existing.status = AcquisitionStatus.BLOCKED
                existing.blocked_reason = "ACQUISITION_CYCLE"
                existing.completed_at = utcnow()
                ctx.record_acquisition_session(existing)
                return ctx.complete(
                    output={"shared": False, "blocked": True, "reason": "ACQUISITION_CYCLE"},
                    emitted_events=[
                        ctx.new_event(EXTENSION_REVIEWED, {
                            "proposal_id": str(proposal.id), "decision": "reject",
                            "approval_source": ApprovalSource.AUTONOMY_POLICY.value,
                            "acquisition_session_id": str(existing.id),
                            "reason": "ACQUISITION_CYCLE",
                        }),
                        ctx.new_event(ACQUISITION_BLOCKED, _session_payload(existing)),
                    ],
                )
            visited.add(ancestor.id)
            ancestor = store.get_session(ancestor.parent_session_id) if ancestor.parent_session_id else None
        if existing.extension_proposal_id != proposal.id and proposal.status is ExtensionProposalStatus.REVIEW:
            return ctx.complete(
                output={"shared": True, "acquisition_session_id": str(existing.id)},
                emitted_events=[ctx.new_event(EXTENSION_REVIEWED, {
                    "proposal_id": str(proposal.id), "decision": "reject",
                    "approval_source": "SHARED_ACQUISITION",
                    "acquisition_session_id": str(existing.id),
                    "reason": "an existing logical acquisition serves this WorkRequirement",
                })],
            )
        return ctx.complete(output={"shared": True, "acquisition_session_id": str(existing.id)})

    budget = ctx.services.get_default_autonomy_budget()
    parent_id = _uuid(work.metadata.get("parent_acquisition_session_id"))
    parent = store.get_session(parent_id) if parent_id else None
    depth = parent.extension_depth + 1 if parent else int(work.metadata.get("extension_depth", 0))
    session = CapabilityAcquisitionSession(
        capability_gap_id=gap.id, source_work_requirement_id=work.id,
        acquisition_key=key,
        target_capabilities=[item.to_dict() for item in requirements],
        extension_proposal_id=proposal.id, parent_session_id=parent_id,
        extension_depth=depth, autonomy_budget=budget,
        id=subscriber.acquisition_session_id,
    )
    subscriber.id = uuid.uuid5(session.id, f"subscriber:{work.id}")
    ctx.record_acquisition_subscriber(subscriber)

    reason = _opening_budget_reason(store, session, work)
    if reason:
        session.status = AcquisitionStatus.BLOCKED
        session.current_stage = AcquisitionStage.EXTENSION
        session.blocked_reason = reason
        ctx.record_acquisition_session(session)
        return ctx.complete(
            output={"opened": True, "blocked": True, "reason": reason},
            emitted_events=[
                ctx.new_event(EXTENSION_REVIEWED, {
                    "proposal_id": str(proposal.id), "decision": "reject",
                    "approval_source": ApprovalSource.AUTONOMY_POLICY.value,
                    "acquisition_session_id": str(session.id), "reason": reason,
                }),
                ctx.new_event(ACQUISITION_BLOCKED, _session_payload(session)),
            ],
        )
    return _evaluate_extension(ctx, session, proposal, opened=True)


def _gap_opened(ctx: ProcessContext) -> ProcessResult:
    """Detect an ancestral acquisition cycle before proposal dedup can hide it."""

    gap = ctx.services.get_capability_gap(_uuid(ctx.event.payload.get("capability_gap_id")))
    work = ctx.services.get_work_requirement(gap.work_requirement_id) if gap else None
    parent_id = _uuid(work.metadata.get("parent_acquisition_session_id")) if work else None
    if gap is None or parent_id is None:
        return ctx.complete(output={"checked": False})
    key = acquisition_key_for(gap.missing_capabilities or gap.required_capabilities)
    existing = ctx.services.find_acquisition_by_key(key)
    ancestor = ctx.services.get_acquisition_session(parent_id)
    visited: set[uuid.UUID] = set()
    while ancestor is not None and ancestor.id not in visited:
        if existing is not None and ancestor.id == existing.id:
            existing.status = AcquisitionStatus.BLOCKED
            existing.blocked_reason = "ACQUISITION_CYCLE"
            existing.completed_at = utcnow()
            ctx.record_acquisition_session(existing)
            return ctx.complete(
                output={"checked": True, "cycle": True},
                emitted_events=[ctx.new_event(ACQUISITION_BLOCKED, _session_payload(existing))],
            )
        visited.add(ancestor.id)
        ancestor = (
            ctx.services.get_acquisition_session(ancestor.parent_session_id)
            if ancestor.parent_session_id else None
        )
    return ctx.complete(output={"checked": True, "cycle": False})


def _open_from_approved(ctx: ProcessContext) -> ProcessResult:
    proposal_id = _uuid(ctx.event.payload.get("extension_proposal_id")) or _uuid(ctx.event.payload.get("proposal_id"))
    proposal = ctx.services.get_extension_proposal(proposal_id)
    gap = ctx.services.get_capability_gap(proposal.capability_gap_id) if proposal else None
    if proposal is None or gap is None:
        return ctx.fail("approved extension provenance missing")
    key = acquisition_key_for(gap.missing_capabilities or gap.required_capabilities)
    existing = ctx.services.find_acquisition_by_key(key)
    if existing:
        return ctx.complete(output={"opened": False, "shared": True})
    session = CapabilityAcquisitionSession(
        capability_gap_id=gap.id, source_work_requirement_id=gap.work_requirement_id,
        acquisition_key=key,
        target_capabilities=[item.to_dict() for item in gap.missing_capabilities],
        extension_proposal_id=proposal.id, status=AcquisitionStatus.CONSTRUCTING,
        current_stage=AcquisitionStage.CONSTRUCTION,
        autonomy_budget=ctx.services.get_default_autonomy_budget(),
    )
    ctx.record_acquisition_session(session)
    ctx.record_acquisition_subscriber(AcquisitionSubscriber(
        acquisition_session_id=session.id, work_requirement_id=gap.work_requirement_id,
        id=uuid.uuid5(session.id, f"subscriber:{gap.work_requirement_id}"),
    ))
    return ctx.complete(output={"opened": True, "acquisition_session_id": str(session.id)})


def _evaluate_extension(ctx, session, proposal, *, opened: bool) -> ProcessResult:
    evaluation = ctx.services.get_autonomy_policy().evaluate_extension(proposal)
    ctx.record_autonomy_decision(
        _decision_record(ctx, session, AcquisitionStage.EXTENSION, evaluation)
    )
    events = [ctx.new_event(ACQUISITION_OPENED, _session_payload(session))] if opened else []
    if evaluation.decision is AutonomyDecisionKind.FORBIDDEN:
        session.current_stage = AcquisitionStage.EXTENSION
        session.status = AcquisitionStatus.BLOCKED
        session.blocked_reason = "AUTONOMY_FORBIDDEN: " + "; ".join(evaluation.reasons)
        session.review_summary = _review_summary(session, proposal, evaluation)
        ctx.record_acquisition_session(session)
        events.extend([
            ctx.new_event(EXTENSION_REVIEWED, {
                "proposal_id": str(proposal.id), "decision": "reject",
                "approval_source": ApprovalSource.AUTONOMY_POLICY.value,
                "acquisition_session_id": str(session.id), "reason": session.blocked_reason,
            }),
            ctx.new_event(ACQUISITION_BLOCKED, _session_payload(session)),
        ])
        return ctx.complete(output={"decision": evaluation.decision.value}, emitted_events=events)
    budget_reason = _proposal_budget_reason(session, proposal, evaluation.risk)
    if budget_reason:
        session.current_stage = AcquisitionStage.EXTENSION
        session.status = AcquisitionStatus.BLOCKED
        session.blocked_reason = budget_reason
        ctx.record_acquisition_session(session)
        events.extend([
            ctx.new_event(EXTENSION_REVIEWED, {
                "proposal_id": str(proposal.id), "decision": "reject",
                "approval_source": ApprovalSource.AUTONOMY_POLICY.value,
                "acquisition_session_id": str(session.id), "reason": budget_reason,
            }),
            ctx.new_event(ACQUISITION_BLOCKED, _session_payload(session)),
        ])
        return ctx.complete(output={"decision": "BUDGET_BLOCK", "reason": budget_reason}, emitted_events=events)
    missing_dependencies = _missing_dependencies(ctx, proposal)
    if missing_dependencies:
        session.status = AcquisitionStatus.ANALYZING
        session.current_stage = AcquisitionStage.EXTENSION
        session.review_summary = {
            "dependency_capabilities": list(missing_dependencies),
            "extension_depth": session.extension_depth,
            "autonomy_budget": session.autonomy_budget.to_dict(),
        }
        ctx.record_acquisition_session(session)
        for name in missing_dependencies:
            dependency_hints = {}
            for component in proposal.proposed_components:
                if name in component.requires_capabilities:
                    dependency_hints.update(
                        dict((component.metadata or {}).get("dependency_hints", {}).get(name, {}))
                    )
            requirement = ctx.require_work(
                work_type="capability_acquisition_dependency",
                work_key=f"acquisition-dependency:{session.id}:{name}",
                reason=f"extension {proposal.id} requires {name} before construction",
                metadata={
                    "parent_acquisition_session_id": str(session.id),
                    "extension_depth": session.extension_depth + 1,
                },
                required_capabilities=[CapabilityRequirement(
                    name=name,
                    metadata={"extension": dependency_hints} if dependency_hints else {},
                )],
            )
            events.append(ctx.new_event(
                "work_required", {"work_requirement_id": str(requirement.id)}
            ))
        return ctx.complete(
            output={"waiting_for_dependencies": missing_dependencies},
            emitted_events=events,
        )
    session.current_stage = AcquisitionStage.EXTENSION
    session.review_summary = _review_summary(session, proposal, evaluation)
    if evaluation.decision is AutonomyDecisionKind.AUTO:
        session.status = AcquisitionStatus.CONSTRUCTING
        ctx.record_acquisition_session(session)
        events.append(ctx.new_event(EXTENSION_REVIEWED, {
            "proposal_id": str(proposal.id), "decision": "approve",
            "approval_source": ApprovalSource.AUTONOMY_POLICY.value,
            "acquisition_session_id": str(session.id),
        }))
        return ctx.complete(output={"decision": "AUTO"}, emitted_events=events)
    session.status = AcquisitionStatus.WAITING_REVIEW
    ctx.record_acquisition_session(session)
    return ctx.suspend(
        resume_point="await_autonomy_review",
        waiting_for={"event_type": AUTONOMY_REVIEWED, "acquisition_session_id": str(session.id)},
        saved_process_state={
            "acquisition_session_id": str(session.id), "stage": AcquisitionStage.EXTENSION.value,
            "extension_proposal_id": str(proposal.id),
        },
        emitted_events=events,
    )


def _installation_review(ctx, session) -> ProcessResult:
    plan_id = _uuid(ctx.event.payload.get("installation_plan_id"))
    plan = ctx.services.get_installation_plan(plan_id)
    proposal = ctx.services.get_extension_proposal(session.extension_proposal_id)
    if plan is None or proposal is None:
        return ctx.fail("installation review provenance missing")
    session.installation_plan_id = plan.id
    session.installation_attempts = max(1, session.installation_attempts)
    if session.installation_attempts > session.autonomy_budget.max_installation_attempts:
        return _block(ctx, session, "INSTALLATION_BUDGET_EXHAUSTED")
    session.current_stage = AcquisitionStage.INSTALLATION
    evaluation = ctx.services.get_autonomy_policy().evaluate_installation(plan, proposal)
    session.review_summary = _review_summary(session, proposal, evaluation, installation_plan=plan)
    ctx.record_autonomy_decision(_decision_record(ctx, session, AcquisitionStage.INSTALLATION, evaluation))
    ctx.record_acquisition_attempt(AcquisitionAttempt(
        acquisition_session_id=session.id, attempt_type="INSTALLATION",
        attempt_number=session.installation_attempts, plan_id=plan.id,
        id=uuid.uuid5(session.id, f"installation:{session.installation_attempts}"),
    ))
    if evaluation.decision is AutonomyDecisionKind.FORBIDDEN:
        session.status = AcquisitionStatus.BLOCKED
        session.blocked_reason = "AUTONOMY_FORBIDDEN: " + "; ".join(evaluation.reasons)
        ctx.record_acquisition_session(session)
        return ctx.complete(emitted_events=[
            ctx.new_event(INSTALLATION_REVIEWED, {
                "installation_plan_id": str(plan.id), "decision": "reject",
                "approval_source": ApprovalSource.AUTONOMY_POLICY.value,
                "acquisition_session_id": str(session.id),
            }),
            ctx.new_event(ACQUISITION_BLOCKED, _session_payload(session)),
        ])
    if evaluation.decision is AutonomyDecisionKind.AUTO:
        session.status = AcquisitionStatus.INSTALLING
        ctx.record_acquisition_session(session)
        return ctx.complete(
            output={"decision": "AUTO"},
            emitted_events=[ctx.new_event(INSTALLATION_REVIEWED, {
                "installation_plan_id": str(plan.id), "decision": "approve",
                "approval_source": ApprovalSource.AUTONOMY_POLICY.value,
                "acquisition_session_id": str(session.id),
            })],
        )
    session.status = AcquisitionStatus.WAITING_REVIEW
    ctx.record_acquisition_session(session)
    return ctx.suspend(
        resume_point="await_autonomy_review",
        waiting_for={"event_type": AUTONOMY_REVIEWED, "acquisition_session_id": str(session.id)},
        saved_process_state={
            "acquisition_session_id": str(session.id), "stage": AcquisitionStage.INSTALLATION.value,
            "installation_plan_id": str(plan.id),
        },
    )


def _resume_review(ctx: ProcessContext) -> ProcessResult:
    assert ctx.event is not None and ctx.services is not None
    session_id = _uuid(ctx.saved_process_state.get("acquisition_session_id"))
    session = ctx.services.get_acquisition_session(session_id)
    if session is None:
        return ctx.fail("acquisition session disappeared during review")
    stage = AcquisitionStage(ctx.saved_process_state["stage"])
    decision = str(ctx.event.payload.get("decision", "reject")).lower()
    if stage is AcquisitionStage.EXTENSION:
        proposal = ctx.services.get_extension_proposal(session.extension_proposal_id)
        evaluation = ctx.services.get_autonomy_policy().evaluate_extension(proposal)
        event_type = EXTENSION_REVIEWED
        target = {"proposal_id": str(proposal.id)}
        next_status = AcquisitionStatus.CONSTRUCTING
    else:
        plan = ctx.services.get_installation_plan(session.installation_plan_id)
        proposal = ctx.services.get_extension_proposal(session.extension_proposal_id)
        evaluation = ctx.services.get_autonomy_policy().evaluate_installation(plan, proposal)
        event_type = INSTALLATION_REVIEWED
        target = {"installation_plan_id": str(plan.id)}
        next_status = AcquisitionStatus.INSTALLING
    budget_reason = _proposal_budget_reason(session, proposal, evaluation.risk)
    if evaluation.decision is AutonomyDecisionKind.FORBIDDEN:
        decision = "reject"
        session.blocked_reason = "AUTONOMY_FORBIDDEN: " + "; ".join(evaluation.reasons)
    elif budget_reason:
        decision = "reject"
        session.blocked_reason = budget_reason
    elif decision != "approve":
        session.blocked_reason = "REJECTED_BY_HUMAN"
    session.status = next_status if decision == "approve" else AcquisitionStatus.BLOCKED
    ctx.record_acquisition_session(session)
    ctx.record_autonomy_decision(AutonomyDecision(
        acquisition_session_id=session.id, stage=stage,
        decision=(AutonomyDecisionKind.REVIEW_REQUIRED if evaluation.decision is not AutonomyDecisionKind.FORBIDDEN else AutonomyDecisionKind.FORBIDDEN),
        evaluated_risk=evaluation.risk, permissions=list(evaluation.permissions),
        production_impact=evaluation.production_impact,
        rollback_available=evaluation.rollback_available,
        reasons=[f"human decision: {decision}", *evaluation.reasons],
        policy_name=ctx.services.get_autonomy_policy().name,
        policy_version=ctx.services.get_autonomy_policy().version,
        budget_snapshot=session.autonomy_budget.to_dict(),
        decision_key=f"{stage.value}:human:{ctx.event.id}",
        decided_by_process_id=ctx.instance.id, reviewed_by_event_id=ctx.event.id,
    ))
    events = [ctx.new_event(event_type, {
        **target, "decision": decision, "approval_source": ApprovalSource.HUMAN.value,
        "acquisition_session_id": str(session.id),
    })]
    if decision != "approve":
        events.append(ctx.new_event(ACQUISITION_BLOCKED, _session_payload(session)))
    return ctx.complete(output={"approved": decision == "approve"}, emitted_events=events)


def _construction_failed(ctx, session) -> ProcessResult:
    plan_id = _uuid(ctx.event.payload.get("construction_plan_id"))
    result = ctx.services.get_construction_result(plan_id) if plan_id else None
    attempt_number = max(1, session.construction_attempts)
    failure = ctx.event.payload.get("failure_reason") or "; ".join(ctx.event.payload.get("reasons") or []) or "construction failed"
    status = getattr(getattr(result, "status", None), "value", ctx.event.payload.get("status", "FAILED"))
    ctx.record_acquisition_attempt(AcquisitionAttempt(
        acquisition_session_id=session.id, attempt_type="CONSTRUCTION",
        attempt_number=attempt_number, plan_id=plan_id,
        result_id=getattr(result, "id", None), status=status,
        failure_reason=failure, completed_at=utcnow(),
        id=uuid.uuid5(session.id, f"construction:{attempt_number}"),
    ))
    if status == ConstructionResultStatus.BLOCKED.value:
        return _block(ctx, session, "CONSTRUCTION_BLOCKED")
    if attempt_number >= session.autonomy_budget.max_construction_attempts:
        return _block(ctx, session, "CONSTRUCTION_BUDGET_EXHAUSTED")
    next_attempt = attempt_number + 1
    session.construction_attempts = next_attempt
    session.construction_plan_id = None
    session.status = AcquisitionStatus.CONSTRUCTING
    session.current_stage = AcquisitionStage.CONSTRUCTION
    ctx.record_acquisition_session(session)
    return ctx.complete(
        output={"retry": True, "attempt": next_attempt},
        emitted_events=[ctx.new_event(CONSTRUCTION_RETRY_REQUESTED, {
            "extension_proposal_id": str(session.extension_proposal_id),
            "acquisition_session_id": str(session.id),
            "construction_attempt": next_attempt, "previous_plan_id": str(plan_id),
            "failure_reason": failure,
        })],
    )


def _installation_rolled_back(ctx, session) -> ProcessResult:
    """Bound an installation redesign; retry through construction, never in place."""

    attempt_number = max(1, session.installation_attempts)
    ctx.record_acquisition_attempt(AcquisitionAttempt(
        acquisition_session_id=session.id, attempt_type="INSTALLATION",
        attempt_number=attempt_number, plan_id=session.installation_plan_id,
        status="ROLLED_BACK", failure_reason="installation failed and rolled back",
        completed_at=utcnow(),
        id=uuid.uuid5(session.id, f"installation:{attempt_number}"),
    ))
    if attempt_number >= session.autonomy_budget.max_installation_attempts:
        return _block(ctx, session, "INSTALLATION_BUDGET_EXHAUSTED")
    if session.construction_attempts >= session.autonomy_budget.max_construction_attempts:
        return _block(ctx, session, "CONSTRUCTION_BUDGET_EXHAUSTED")
    next_construction = max(1, session.construction_attempts) + 1
    session.installation_attempts = attempt_number + 1
    session.construction_attempts = next_construction
    session.status = AcquisitionStatus.CONSTRUCTING
    session.current_stage = AcquisitionStage.CONSTRUCTION
    ctx.record_acquisition_session(session)
    return ctx.complete(
        output={
            "retry": True,
            "installation_attempt": session.installation_attempts,
            "construction_attempt": next_construction,
        },
        emitted_events=[ctx.new_event(CONSTRUCTION_RETRY_REQUESTED, {
            "extension_proposal_id": str(session.extension_proposal_id),
            "acquisition_session_id": str(session.id),
            "construction_attempt": next_construction,
            "previous_plan_id": str(session.construction_plan_id),
            "retry_reason_stage": "INSTALLATION",
            "failure_reason": "previous installation rolled back",
        })],
    )


def _work_finished(ctx) -> ProcessResult:
    work_id = _uuid(ctx.event.payload.get("work_requirement_id"))
    if work_id is None:
        return ctx.complete(output={"updated": False})
    store = ctx.services.get_autonomy_store()
    sessions = store.sessions_for_work(work_id)
    changed = []
    for session in sessions:
        subscriber = AcquisitionSubscriber(
            acquisition_session_id=session.id, work_requirement_id=work_id,
            status="SATISFIED" if ctx.event.type == "work_satisfied" else "CANCELLED",
            id=uuid.uuid5(session.id, f"subscriber:{work_id}"),
        )
        ctx.record_acquisition_subscriber(subscriber)
        other = [s for s in store.subscribers(session.id, active_only=True) if s.work_requirement_id != work_id]
        if not other and not session.status.terminal:
            session.status = AcquisitionStatus.CANCELLED
            session.blocked_reason = "NO_ACTIVE_SUBSCRIBERS"
            session.completed_at = utcnow()
            ctx.record_acquisition_session(session)
            for continuation in ctx.services.find_autonomy_review_continuations(session.id):
                ctx.close_continuation(
                    continuation.id,
                    process_instance_id=continuation.process_instance_id,
                    reason="capability acquisition no longer has an active subscriber",
                )
        changed.append(str(session.id))
    work = ctx.services.get_work_requirement(work_id)
    events = []
    parent_id = _uuid(work.metadata.get("parent_acquisition_session_id")) if work else None
    if parent_id and ctx.event.type == "work_satisfied":
        events.append(ctx.new_event(ACQUISITION_DEPENDENCY_SATISFIED, {
            "acquisition_session_id": str(parent_id),
            "dependency_work_requirement_id": str(work_id),
        }))
    return ctx.complete(output={"updated": changed}, emitted_events=events)


def _dependency_satisfied(ctx) -> ProcessResult:
    session = ctx.services.get_acquisition_session(
        _uuid(ctx.event.payload.get("acquisition_session_id"))
    )
    if session is None or session.status.terminal:
        return ctx.complete(output={"advanced": False})
    proposal = ctx.services.get_extension_proposal(session.extension_proposal_id)
    if proposal is None:
        return ctx.fail("parent extension proposal disappeared")
    remaining = _missing_dependencies(ctx, proposal)
    if remaining:
        return ctx.complete(output={"advanced": False, "remaining_dependencies": remaining})
    return _evaluate_extension(ctx, session, proposal, opened=False)


def _missing_dependencies(ctx, proposal) -> list[str]:
    registry = ctx.services.get_capability_registry()
    required = sorted({
        name
        for component in proposal.proposed_components
        for name in component.requires_capabilities
    })
    provided = registry.provided_capability_names()
    return [name for name in required if name not in provided]


def _capability_became_available(ctx) -> ProcessResult:
    installation_plan_id = _uuid(ctx.event.payload.get("installation_plan_id"))
    own_session = None
    if installation_plan_id:
        own_session = ctx.services.find_acquisition_by_installation_plan(installation_plan_id)
        if own_session and not own_session.status.terminal:
            own_session.status = AcquisitionStatus.ACTIVATING
            own_session.current_stage = AcquisitionStage.ACTIVATION
            ctx.record_acquisition_session(own_session)
    capability_name = str(ctx.event.payload.get("capability_name", ""))
    changed = []
    dependency_events = []
    for session in ctx.services.get_autonomy_store().sessions():
        names = {item.get("name") for item in session.target_capabilities}
        if (
            session.status is AcquisitionStatus.ANALYZING
            and session.extension_proposal_id
        ):
            proposal = ctx.services.get_extension_proposal(session.extension_proposal_id)
            dependencies = {
                name
                for component in (proposal.proposed_components if proposal else [])
                for name in component.requires_capabilities
            }
            if capability_name in dependencies:
                dependency_events.append(ctx.new_event(
                    ACQUISITION_DEPENDENCY_SATISFIED,
                    {
                        "acquisition_session_id": str(session.id),
                        "capability_name": capability_name,
                    },
                ))
        if (
            not session.status.terminal
            and capability_name in names
            and (own_session is None or session.id != own_session.id)
        ):
            session.status = AcquisitionStatus.CANCELLED
            session.blocked_reason = "CAPABILITY_ALREADY_AVAILABLE"
            session.completed_at = utcnow()
            ctx.record_acquisition_session(session)
            for continuation in ctx.services.find_autonomy_review_continuations(session.id):
                ctx.close_continuation(
                    continuation.id,
                    process_instance_id=continuation.process_instance_id,
                    reason="target capability became available by another route",
                )
            changed.append(str(session.id))
    return ctx.complete(
        output={"cancelled_as_unnecessary": changed},
        emitted_events=dependency_events,
    )


def _opening_budget_reason(store, session, work) -> str | None:
    if session.extension_depth > session.autonomy_budget.max_extension_depth:
        return "EXTENSION_DEPTH_EXHAUSTED"
    if len(store.sessions_for_work(work.id)) >= session.autonomy_budget.max_extensions_per_work:
        return "EXTENSIONS_PER_WORK_EXHAUSTED"
    ancestor = store.get_session(session.parent_session_id) if session.parent_session_id else None
    names = {item.get("name") for item in session.target_capabilities}
    visited: set[uuid.UUID] = set()
    while ancestor is not None and ancestor.id not in visited:
        visited.add(ancestor.id)
        if names & {item.get("name") for item in ancestor.target_capabilities}:
            return "ACQUISITION_CYCLE"
        ancestor = store.get_session(ancestor.parent_session_id) if ancestor.parent_session_id else None
    return None


def _proposal_budget_reason(session, proposal, risk: str) -> str | None:
    ranks = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    allowed = ranks.get(session.autonomy_budget.allowed_risk, -1)
    if ranks.get(str(risk).upper(), 3) > allowed:
        return "AUTONOMY_RISK_BUDGET_EXHAUSTED"
    if (
        session.autonomy_budget.max_total_cost is not None
        and proposal.estimated_cost is not None
        and float(proposal.estimated_cost) > session.autonomy_budget.max_total_cost
    ):
        return "AUTONOMY_COST_BUDGET_EXHAUSTED"
    if session.autonomy_budget.max_elapsed_seconds is not None:
        now = utcnow()
        created = session.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() > session.autonomy_budget.max_elapsed_seconds:
            return "AUTONOMY_TIME_BUDGET_EXHAUSTED"
    return None


def _session_for_event(ctx):
    payload = ctx.event.payload
    proposal_id = _uuid(payload.get("extension_proposal_id")) or _uuid(payload.get("proposal_id"))
    if proposal_id:
        session = ctx.services.find_acquisition_by_proposal(proposal_id)
        if session:
            return session
    construction_plan_id = _uuid(payload.get("construction_plan_id"))
    if construction_plan_id:
        session = ctx.services.find_acquisition_by_construction_plan(construction_plan_id)
        if session:
            return session
        plan = ctx.services.get_construction_plan(construction_plan_id)
        if plan:
            return ctx.services.find_acquisition_by_proposal(plan.extension_proposal_id)
    installation_plan_id = _uuid(payload.get("installation_plan_id"))
    if installation_plan_id:
        session = ctx.services.find_acquisition_by_installation_plan(installation_plan_id)
        if session:
            return session
        plan = ctx.services.get_installation_plan(installation_plan_id)
        if plan:
            return ctx.services.find_acquisition_by_proposal(plan.extension_proposal_id)
    return None


def _advance(session, ctx, status, stage) -> ProcessResult:
    session.status = status
    session.current_stage = stage
    ctx.record_acquisition_session(session)
    return ctx.complete(output={"advanced": True, "status": status.value, "stage": stage.value})


def _block(ctx, session, reason: str) -> ProcessResult:
    session.status = AcquisitionStatus.BLOCKED
    session.blocked_reason = reason
    session.completed_at = utcnow()
    ctx.record_acquisition_session(session)
    return ctx.complete(
        output={"blocked": True, "reason": reason},
        emitted_events=[ctx.new_event(ACQUISITION_BLOCKED, _session_payload(session))],
    )


def _decision_record(ctx, session, stage, evaluation) -> AutonomyDecision:
    policy = ctx.services.get_autonomy_policy()
    return AutonomyDecision(
        acquisition_session_id=session.id, stage=stage,
        decision=evaluation.decision, evaluated_risk=evaluation.risk,
        permissions=list(evaluation.permissions),
        production_impact=evaluation.production_impact,
        rollback_available=evaluation.rollback_available,
        reasons=list(evaluation.reasons), policy_name=policy.name,
        policy_version=policy.version,
        budget_snapshot=session.autonomy_budget.to_dict(),
        decided_by_process_id=ctx.instance.id,
    )


def _review_summary(session, proposal, evaluation, *, installation_plan=None) -> dict:
    return {
        "acquisition_session_id": str(session.id),
        "source_work_requirement_id": str(session.source_work_requirement_id),
        "target_capabilities": list(session.target_capabilities),
        "strategy": proposal.declared_strategy,
        "proposed_components": [item.to_dict() for item in proposal.proposed_components],
        "evaluated_risk": evaluation.risk,
        "required_permissions": list(evaluation.permissions),
        "production_impact": evaluation.production_impact,
        "verification": "all Phase 5B structural/static/behavior checks required",
        "rollback_available": evaluation.rollback_available,
        "installation_plan_id": str(installation_plan.id) if installation_plan else None,
        "extension_depth": session.extension_depth,
        "autonomy_budget": session.autonomy_budget.to_dict(),
    }


def _session_payload(session) -> dict:
    return {
        "acquisition_session_id": str(session.id),
        "capability_gap_id": str(session.capability_gap_id),
        "work_requirement_id": str(session.source_work_requirement_id),
        "status": session.status.value, "stage": session.current_stage.value,
        "blocked_reason": session.blocked_reason,
    }


def bootstrap_autonomy(runtime) -> None:
    """Register the ordinary Process that coordinates the acquisition loop."""

    from .installation import bootstrap_installation

    bootstrap_installation(runtime)
    runtime.register_process(ADVANCE_CAPABILITY_ACQUISITION, advance_capability_acquisition)


__all__ = [
    "ACQUISITION_BLOCKED", "ACQUISITION_COMPLETED", "ACQUISITION_OPENED",
    "ACQUISITION_DEPENDENCY_SATISFIED",
    "ADVANCE_CAPABILITY_ACQUISITION", "AUTONOMY_REVIEWED",
    "CONSTRUCTION_RETRY_REQUESTED", "advance_capability_acquisition",
    "bootstrap_autonomy",
]
