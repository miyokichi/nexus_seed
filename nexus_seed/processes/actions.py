"""The action pipeline, expressed as ordinary Processes.

Phase 3C adds no Runtime feature and no new primitive: the whole
"may we act, and what happened when we did" boundary is three Processes wired
together by events (spec §44), so the Runtime keeps holding zero domain rules.

    <any process>      -> ActionProposal + action_proposed
    action_proposed    -> action_validator  -> schema / capability / permission
                                               / risk policy
                                            -> action_approved | action_rejected
                                            -> (or SUSPEND for human review)
    action_reviewed    -> action_validator  -> re-validate, then approve/reject
    action_approved    -> action_executor   -> ExecutionBackend
                                            -> ActionExecution journal
                                            -> action_succeeded | action_failed

The proposing process suspends on the *terminal* action events and is resumed by
them, so it never learns how authorization was reached — only how it ended.

Lifecycle events carry both ``action_proposal_id`` and ``root_proposal_id``.
They are equal unless a human replaced the proposal during review, in which case
the root id keeps the original waiter attached to the chain.
"""

from __future__ import annotations

import uuid

from ..actions.models import (
    ActionDecision,
    ActionDecisionRecord,
    ActionExecution,
    ActionExecutionStatus,
    ActionProposal,
    ActionProposalStatus,
    RiskLevel,
)
from ..actions.permissions import granted_permissions
from ..actions.policy import ActionPolicy
from ..actions.validation import validate_action_proposal
from ..backends.action import ActionRequest, capabilities_of, summarize
from ..capabilities.models import CapabilityRef
from ..context.requirements import (
    ContextRequirements,
    ContinuationReq,
    WorkReq,
    WorldStateReq,
)
from ..core.event import Event, utcnow
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult

#: Metadata key holding the risk policy table on the validator's definition.
RISK_POLICY_METADATA_KEY = "risk_policy"

ACTION_VALIDATOR = ProcessDefinition(
    name="action_validator",
    version="1",
    handler="action_validator",
    trigger_event_types=("action_proposed",),
    metadata={"role": "action_validator", RISK_POLICY_METADATA_KEY: ActionPolicy().to_dict()},
    context_requirements=ContextRequirements(
        include_trigger_event=True, continuation=ContinuationReq(include=True)
    ),
)

ACTION_EXECUTOR = ProcessDefinition(
    name="action_executor",
    version="1",
    handler="action_executor",
    trigger_event_types=("action_approved",),
    max_retries=2,
    metadata={"role": "action_executor"},
    context_requirements=ContextRequirements(include_trigger_event=True),
)


def _uuid(value) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _lifecycle_payload(proposal: ActionProposal, **extra) -> dict:
    payload = {
        "action_proposal_id": str(proposal.id),
        "root_proposal_id": str(proposal.root_proposal_id),
        "backend": proposal.backend,
        "action_type": proposal.action_type,
    }
    payload.update(extra)
    return payload


def waiting_for_action(proposal: ActionProposal) -> dict:
    """The ``waiting_for`` a proposing process should suspend on.

    Matches any terminal outcome of the proposal's chain, so the proposer is
    resumed whether the action succeeded, failed or was refused.
    """
    root = str(proposal.root_proposal_id)
    return {
        "any": [
            {"event_type": "action_succeeded", "root_proposal_id": root},
            {"event_type": "action_failed", "root_proposal_id": root},
            {"event_type": "action_rejected", "root_proposal_id": root},
        ]
    }


def action_proposed_event(ctx: ProcessContext, proposal: ActionProposal) -> Event:
    """Build the ``action_proposed`` event that starts authorization."""
    return ctx.new_event(
        "action_proposed",
        _lifecycle_payload(
            proposal,
            risk_level=proposal.risk_level.value,
            created_by_process_id=str(ctx.instance.id),
        ),
    )


# --- validator -------------------------------------------------------------


async def action_validator(ctx: ProcessContext) -> ProcessResult:
    """Authorize (or refuse) one ActionProposal — never execute it."""
    if ctx.resume_point == "await_action_review":
        return _handle_action_review(ctx)

    assert ctx.event is not None
    proposal_id = _uuid(ctx.event.payload.get("action_proposal_id"))
    proposal = ctx.services.get_action_proposal(proposal_id) if ctx.services else None
    if proposal is None:
        return ctx.fail(f"action proposal {proposal_id} not found")
    return _authorize(ctx, proposal)


def _authorize(
    ctx: ProcessContext,
    proposal: ActionProposal,
    *,
    human_approved: bool = False,
    reviewed_by_event_id: uuid.UUID | None = None,
) -> ProcessResult:
    """Run validation + policy and stage the resulting decision.

    ``human_approved`` re-runs every *validation* stage but skips the risk
    gate — a human already answered that question.  Re-validating on approval is
    the point of spec §22: permissions, backends and state may have moved while
    the proposal sat in review.
    """
    definition = None
    creator = (
        ctx.services.get_process_instance(proposal.created_by_process_id)
        if ctx.services and proposal.created_by_process_id
        else None
    )
    if creator is not None and ctx.services is not None:
        definition = ctx.services.get_definition(
            creator.definition_name, creator.definition_version
        )
    granted = granted_permissions(definition)

    backend = ctx.backends.get(proposal.backend) if ctx.backends else None
    validation = validate_action_proposal(
        proposal,
        capabilities=capabilities_of(backend),
        granted_permissions=granted,
    )

    policy = _policy_for(ctx)
    if not validation.ok:
        decision = ActionDecision.REJECT
    elif human_approved:
        decision = ActionDecision.APPROVE
    else:
        decision = policy.decide(proposal.risk_level)

    ctx.record_action_decision(
        ActionDecisionRecord(
            action_proposal_id=proposal.id,
            decision=decision,
            risk_level=proposal.risk_level,
            decided_by_process_id=ctx.instance.id,
            process_definition_name=definition.name if definition else None,
            process_definition_version=definition.version if definition else None,
            granted_permissions=granted,
            required_permissions=list(proposal.required_permissions),
            mandatory_permissions=validation.mandatory_permissions,
            reasons=list(validation.reasons),
            policy=policy.to_dict(),
            reviewed_by_event_id=reviewed_by_event_id,
        )
    )
    ctx.logger.info(
        "action proposal %s: %s (risk=%s, backend=%s, action_type=%s, reasons=%s)",
        proposal.id,
        decision.value,
        proposal.risk_level.value,
        proposal.backend,
        proposal.action_type,
        validation.reasons,
    )

    if decision is ActionDecision.APPROVE:
        ctx.update_action_proposal(proposal.id, ActionProposalStatus.APPROVED)
        return ctx.complete(
            output={"decision": decision.value, "action_proposal_id": str(proposal.id)},
            emitted_events=[
                ctx.new_event("action_approved", _lifecycle_payload(proposal))
            ],
        )

    if decision is ActionDecision.REVIEW:
        ctx.update_action_proposal(proposal.id, ActionProposalStatus.REVIEW)
        return ctx.suspend(
            resume_point="await_action_review",
            waiting_for={
                "event_type": "action_reviewed",
                "proposal_id": str(proposal.id),
            },
            saved_process_state={"action_proposal_id": str(proposal.id)},
        )

    ctx.update_action_proposal(proposal.id, ActionProposalStatus.REJECTED)
    return ctx.complete(
        output={"decision": decision.value, "reasons": validation.reasons},
        emitted_events=[
            ctx.new_event(
                "action_rejected",
                _lifecycle_payload(proposal, reasons=list(validation.reasons)),
            )
        ],
    )


def _policy_for(ctx: ProcessContext) -> ActionPolicy:
    """Read the risk policy off this process's own definition metadata.

    Policy is configuration, so it travels with the definition in SQLite rather
    than being compiled into the handler or the Runtime (spec §18).
    """
    if ctx.services is None:
        return ActionPolicy()
    definition = ctx.services.get_definition(
        ctx.instance.definition_name, ctx.instance.definition_version
    )
    metadata = definition.metadata if definition else {}
    return ActionPolicy.from_dict(metadata.get(RISK_POLICY_METADATA_KEY))


def _handle_action_review(ctx: ProcessContext) -> ProcessResult:
    """Apply a human's ``action_reviewed`` decision (spec §21–§24)."""
    assert ctx.event is not None
    payload = ctx.event.payload
    proposal_id = _uuid(payload.get("proposal_id")) or _uuid(
        ctx.saved_process_state.get("action_proposal_id")
    )
    human_decision = str(payload.get("decision", "reject")).lower()

    proposal = ctx.services.get_action_proposal(proposal_id) if ctx.services else None
    if proposal is None:
        return ctx.fail(f"action proposal {proposal_id} not found on review")

    if human_decision == "approve":
        # Never trust the earlier verdict: re-validate against the world as it
        # is now, not as it was when the proposal was written (spec §22).
        return _authorize(
            ctx, proposal, human_approved=True, reviewed_by_event_id=ctx.event.id
        )

    if human_decision == "modify":
        return _handle_modify(ctx, proposal, payload.get("replacement"))

    ctx.update_action_proposal(proposal.id, ActionProposalStatus.REJECTED)
    ctx.record_action_decision(
        ActionDecisionRecord(
            action_proposal_id=proposal.id,
            decision=ActionDecision.REJECT,
            risk_level=proposal.risk_level,
            decided_by_process_id=ctx.instance.id,
            reasons=["rejected by human review"],
            reviewed_by_event_id=ctx.event.id,
        )
    )
    return ctx.complete(
        output={"decision": "REJECT", "action_proposal_id": str(proposal.id)},
        emitted_events=[
            ctx.new_event(
                "action_rejected",
                _lifecycle_payload(proposal, reasons=["rejected by human review"]),
            )
        ],
    )


def _handle_modify(ctx: ProcessContext, proposal: ActionProposal, replacement) -> ProcessResult:
    """Replace a reviewed proposal with a human-supplied one (spec §24).

    The replacement is a *new* PENDING proposal that re-enters validation from
    the top — a human edit never yields a directly-approved action.
    """
    assert ctx.event is not None
    new_proposal = ActionProposal.from_dict(
        replacement,
        created_by_process_id=proposal.created_by_process_id,
        trigger_event_id=proposal.trigger_event_id,
        root_proposal_id=proposal.root_proposal_id,
        replaces_proposal_id=proposal.id,
    )
    if new_proposal is None:
        return ctx.fail("modify replacement is not a valid action proposal")

    new_proposal.source_work_requirement_id = proposal.source_work_requirement_id
    new_proposal.context_snapshot_id = proposal.context_snapshot_id
    ctx.add_action_proposal(new_proposal)
    ctx.update_action_proposal(proposal.id, ActionProposalStatus.CANCELLED)
    ctx.record_action_decision(
        ActionDecisionRecord(
            action_proposal_id=proposal.id,
            decision=ActionDecision.REJECT,
            risk_level=proposal.risk_level,
            decided_by_process_id=ctx.instance.id,
            reasons=[f"superseded by {new_proposal.id}"],
            reviewed_by_event_id=ctx.event.id,
        )
    )
    return ctx.complete(
        output={
            "decision": "MODIFIED",
            "action_proposal_id": str(new_proposal.id),
            "replaces": str(proposal.id),
        },
        emitted_events=[
            ctx.new_event(
                "action_proposed",
                _lifecycle_payload(
                    new_proposal,
                    risk_level=new_proposal.risk_level.value,
                    replaces_proposal_id=str(proposal.id),
                ),
            )
        ],
    )


# --- executor --------------------------------------------------------------


async def action_executor(ctx: ProcessContext) -> ProcessResult:
    """Perform one APPROVED action against its backend, and journal the attempt."""
    assert ctx.event is not None
    proposal_id = _uuid(ctx.event.payload.get("action_proposal_id"))
    proposal = ctx.services.get_action_proposal(proposal_id) if ctx.services else None
    if proposal is None:
        return ctx.fail(f"action proposal {proposal_id} not found")

    attempt = ctx.instance.retry_count + 1

    # --- idempotency guard (Invariant 27 / spec §33-§34) ---
    # Checked before the status guard: a SUCCEEDED execution is the strongest
    # evidence there is that the world was already changed, whatever the
    # proposal's status now says.
    already = (
        ctx.services.find_succeeded_action_execution(proposal.idempotency_key)
        if ctx.services
        else None
    )
    if already is not None:
        ctx.logger.info(
            "action proposal %s already executed as %s; skipping backend call",
            proposal.id,
            already.id,
        )
        ctx.record_action_execution(
            ActionExecution(
                action_proposal_id=proposal.id,
                process_instance_id=ctx.instance.id,
                backend=proposal.backend,
                action_type=proposal.action_type,
                status=ActionExecutionStatus.SKIPPED,
                attempt=attempt,
                idempotency_key=proposal.idempotency_key,
                result={"duplicate_of": str(already.id)},
                completed_at=utcnow(),
            )
        )
        # Deliberately emits nothing: the effect already produced its
        # action_succeeded event, and exactly one must exist (spec §60).
        return ctx.complete(
            output={"executed": False, "reason": "duplicate", "of": str(already.id)}
        )

    # Only an approved proposal may reach a backend.  A restart replays this
    # check against SQLite, so a proposal that was rejected or is still in
    # review cannot slip through on a stale in-flight event.
    if proposal.status not in (
        ActionProposalStatus.APPROVED,
        ActionProposalStatus.EXECUTING,
    ):
        ctx.logger.warning(
            "refusing to execute proposal %s in status %s",
            proposal.id,
            proposal.status.value,
        )
        return ctx.complete(
            output={"executed": False, "reason": f"status {proposal.status.value}"}
        )

    backend = ctx.backends.get(proposal.backend) if ctx.backends else None
    if backend is None:
        return _fail_execution(
            ctx,
            proposal,
            attempt,
            f"backend {proposal.backend!r} is not registered",
            retryable=False,
        )

    execution = ActionExecution(
        action_proposal_id=proposal.id,
        process_instance_id=ctx.instance.id,
        backend=proposal.backend,
        action_type=proposal.action_type,
        status=ActionExecutionStatus.STARTED,
        attempt=attempt,
        idempotency_key=proposal.idempotency_key,
    )
    ctx.logger.info(
        "executing action proposal %s attempt %d (backend=%s, action_type=%s, "
        "execution=%s, idempotency_key=%s)",
        proposal.id,
        attempt,
        proposal.backend,
        proposal.action_type,
        execution.id,
        proposal.idempotency_key,
    )

    result = await backend.execute(
        ActionRequest(
            action_type=proposal.action_type,
            target=proposal.target,
            parameters=dict(proposal.parameters),
            idempotency_key=proposal.idempotency_key,
            metadata={
                "action_proposal_id": str(proposal.id),
                "action_execution_id": str(execution.id),
                "process_instance_id": str(ctx.instance.id),
            },
        )
    )
    execution.completed_at = utcnow()

    if not result.success:
        execution.status = ActionExecutionStatus.FAILED
        execution.error = result.error
        ctx.record_action_execution(execution)
        return _after_failed_attempt(ctx, proposal, result, execution)

    execution.status = ActionExecutionStatus.SUCCEEDED
    execution.result = summarize(result.output)
    ctx.record_action_execution(execution)
    ctx.update_action_proposal(proposal.id, ActionProposalStatus.SUCCEEDED)

    succeeded = ctx.new_event(
        "action_succeeded",
        _lifecycle_payload(
            proposal,
            action_execution_id=str(execution.id),
            result=summarize(result.output),
            duplicate=result.duplicate,
        ),
    )
    return ctx.complete(
        output={
            "executed": True,
            "action_execution_id": str(execution.id),
            "action_proposal_id": str(proposal.id),
        },
        emitted_events=[succeeded],
    )


def _after_failed_attempt(
    ctx: ProcessContext, proposal: ActionProposal, result, execution: ActionExecution
) -> ProcessResult:
    """Retry a transient failure, or give up and report it as an event."""
    if result.retryable and ctx.instance.retry_count < ctx.instance.max_retries:
        # The Phase 2A retry loop owns the backoff; the journal entry staged
        # above survives the rollback so the attempt stays auditable.
        return ctx.retry(result.error)
    return _fail_execution(
        ctx, proposal, execution.attempt, result.error, execution=execution
    )


def _fail_execution(
    ctx: ProcessContext,
    proposal: ActionProposal,
    attempt: int,
    error: str | None,
    *,
    retryable: bool = False,
    execution: ActionExecution | None = None,
) -> ProcessResult:
    """Record a terminal failure and report it as an ``action_failed`` event.

    The process itself COMPLETES: the action failed, but *handling* the failure
    succeeded, and the proposing process must be woken rather than left waiting.
    """
    if execution is None:
        execution = ActionExecution(
            action_proposal_id=proposal.id,
            process_instance_id=ctx.instance.id,
            backend=proposal.backend,
            action_type=proposal.action_type,
            status=ActionExecutionStatus.FAILED,
            attempt=attempt,
            idempotency_key=proposal.idempotency_key,
            error=error,
            completed_at=utcnow(),
        )
        ctx.record_action_execution(execution)

    ctx.update_action_proposal(proposal.id, ActionProposalStatus.FAILED)
    ctx.logger.error(
        "action proposal %s failed after %d attempt(s): %s", proposal.id, attempt, error
    )
    failed = ctx.new_event(
        "action_failed",
        _lifecycle_payload(
            proposal,
            action_execution_id=str(execution.id),
            error=error,
            attempts=attempt,
        ),
    )
    return ctx.complete(
        output={"executed": False, "error": error, "attempts": attempt},
        emitted_events=[failed],
    )


# --- an action-performing work process -------------------------------------

WRITE_ANALYSIS_RESULT = ProcessDefinition(
    name="write_analysis_result",
    version="1",
    handler="write_analysis_result",
    metadata={
        "role": "work",
        # This is the whole permission grant for this process: it may write
        # files and nothing else.
        "permissions": ["filesystem.write"],
    },
    provides_capabilities=(CapabilityRef("generate_analysis_report"),),
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        world_state=WorldStateReq(include_work_entities=True),
        work=WorkReq(current=True),
        continuation=ContinuationReq(include=True),
    ),
)

#: Which backend this work process asks to act for it.
ACTION_BACKEND_NAME = "local_file"


async def write_analysis_result(ctx: ProcessContext) -> ProcessResult:
    """Work process: write an analysis result to a file, via a proposal.

    Note what it does *not* do: it never imports, looks up or calls a file API.
    It states an intention, hands it to the action boundary, and waits for the
    world to report back (Invariants 21–22).
    """
    if ctx.resume_point == "await_action":
        return _finish_action_work(ctx)

    entity = ctx.instance.input.get("entity") or "unknown"
    analysis = ctx.view.get_state(entity, "analysis_result") if ctx.view else None
    target_value = ctx.view.get_state(entity, "target") if ctx.view else None

    proposal = ctx.propose_action(
        backend=ctx.instance.input.get("backend", ACTION_BACKEND_NAME),
        action_type="write_file",
        target=f"{entity}_analysis.txt",
        parameters={
            "content": f"entity={entity}\ntarget={target_value}\nanalysis={analysis}\n"
        },
        required_permissions=["filesystem.write"],
        declared_side_effects=["filesystem_write"],
        risk_level=ctx.instance.input.get("risk_level", "LOW"),
        rationale=f"persist the analysis result for {entity}",
    )
    ctx.logger.info(
        "proposing action %s for work %s", proposal.id, ctx.instance.work_key
    )
    return ctx.suspend(
        resume_point="await_action",
        waiting_for=waiting_for_action(proposal),
        saved_process_state={"action_proposal_id": str(proposal.id), "entity": entity},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


def _finish_action_work(ctx: ProcessContext) -> ProcessResult:
    """Complete the work once the action boundary reports an outcome.

    The backend's *result* is not written into World State here (Invariant 25 /
    spec §61); it stays in the ActionExecution journal and in the event payload.
    """
    assert ctx.event is not None
    succeeded = ctx.event.type == "action_succeeded"
    ctx.satisfy_work()
    satisfied = ctx.new_event(
        "work_satisfied",
        {
            "work_requirement_id": str(ctx.instance.work_requirement_id)
            if ctx.instance.work_requirement_id
            else None,
            "work_key": ctx.instance.work_key,
        },
    )
    return ctx.complete(
        output={
            "action_outcome": ctx.event.type,
            "succeeded": succeeded,
            "action_proposal_id": ctx.event.payload.get("action_proposal_id"),
            "action_execution_id": ctx.event.payload.get("action_execution_id"),
        },
        emitted_events=[satisfied],
    )


def bootstrap_actions(runtime, *, policy: ActionPolicy | None = None) -> None:
    """Register the action pipeline (and the demo action work process).

    ``policy`` is persisted on the validator's definition, so a runtime rebuilt
    from the same database keeps the same risk appetite.
    """
    validator = ACTION_VALIDATOR
    if policy is not None:
        validator = ProcessDefinition(
            name=ACTION_VALIDATOR.name,
            version=ACTION_VALIDATOR.version,
            handler=ACTION_VALIDATOR.handler,
            trigger_event_types=ACTION_VALIDATOR.trigger_event_types,
            max_retries=ACTION_VALIDATOR.max_retries,
            metadata={
                **ACTION_VALIDATOR.metadata,
                RISK_POLICY_METADATA_KEY: policy.to_dict(),
            },
            context_requirements=ACTION_VALIDATOR.context_requirements,
        )
    runtime.register_process(validator, action_validator)
    runtime.register_process(ACTION_EXECUTOR, action_executor)
    runtime.register_process(WRITE_ANALYSIS_RESULT, write_analysis_result)


__all__ = [
    "ACTION_BACKEND_NAME",
    "ACTION_EXECUTOR",
    "ACTION_VALIDATOR",
    "RISK_POLICY_METADATA_KEY",
    "RiskLevel",
    "WRITE_ANALYSIS_RESULT",
    "action_executor",
    "action_proposed_event",
    "action_validator",
    "bootstrap_actions",
    "waiting_for_action",
    "write_analysis_result",
]
