"""The self-extension pipeline, expressed as ordinary Processes.

Phase 5A adds no Runtime feature and no new primitive (spec §51): the whole
"what am I missing, and how could I get it" boundary is two Processes wired
together by events.

    capability_missing        -> analyze_capability_gap -> CapabilityGap
                                                        -> AcquisitionCandidates
                                                        -> ExtensionProposal
                                                        -> validation -> policy
                                                        -> extension_proposed
                                                        -> (or SUSPEND for review)
    extension_reviewed        -> analyze_capability_gap -> APPROVED / REJECTED
                                                        -> (or a modified proposal)
    capability_available      -> reconcile_capability_gaps -> gap RESOLVED
    backend_available         -> reconcile_capability_gaps -> re-analysis

What the pipeline never does is the point of the phase.  APPROVED means one
thing: this description may be handed to a construction phase.  No capability is
registered, no ProcessDefinition is added, no file is written, no plugin is
installed and no permission is granted (Invariant 90) — and the gap stays open,
because deciding how to acquire a competence is not acquiring it (Invariant 89).
"""

from __future__ import annotations

import uuid

from ..backends.base import LLMInvocation
from ..capabilities.models import CapabilityRequirement
from ..context.requirements import (
    ContextRequirements,
    ContinuationReq,
    EventsReq,
    WorkReq,
    WorldStateReq,
)
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..work.work_requirement import WorkStatus
from ..extension.builder import build_proposal
from ..extension.models import (
    SOURCE_DETERMINISTIC,
    SOURCE_HUMAN,
    AcquisitionFeasibility,
    CapabilityGap,
    CapabilityGapStatus,
    ExtensionDecision,
    ExtensionDecisionRecord,
    ExtensionProposal,
    ExtensionProposalStatus,
    ExtensionStrategy,
    missing_key_for,
)
from ..extension.policy import ExtensionPolicy
from ..extension.strategies import classify_risk

#: Metadata key holding the extension policy on the analyzer's definition.
#: Policy travels with the definition in SQLite, so a runtime rebuilt from the
#: same database keeps the same appetite for self-extension (spec §42).
EXTENSION_POLICY_METADATA_KEY = "extension_policy"

#: The name a handler looks up in ``ctx.backends`` for the proposing model.
BACKEND_NAME = "llm"

# --- events -----------------------------------------------------------------

CAPABILITY_MISSING = "capability_missing"
CAPABILITY_GAP_OPENED = "capability_gap_opened"
CAPABILITY_GAP_RESOLVED = "capability_gap_resolved"
CAPABILITY_GAP_REANALYSIS_REQUIRED = "capability_gap_reanalysis_required"
CAPABILITY_ACQUISITION_UNAVAILABLE = "capability_acquisition_unavailable"
EXTENSION_PROPOSED = "extension_proposed"
EXTENSION_REVIEW_REQUIRED = "extension_review_required"
EXTENSION_REVIEWED = "extension_reviewed"
EXTENSION_APPROVED = "extension_approved"
EXTENSION_REJECTED = "extension_rejected"
EXTENSION_PROPOSAL_INVALID = "extension_proposal_invalid"
BACKEND_AVAILABLE = "backend_available"
EXTENSION_ENVIRONMENT_CHANGED = "extension_environment_changed"


ANALYZE_CAPABILITY_GAP = ProcessDefinition(
    name="analyze_capability_gap",
    version="1",
    handler="analyze_capability_gap",
    trigger_event_types=(CAPABILITY_MISSING, CAPABILITY_GAP_REANALYSIS_REQUIRED),
    max_retries=2,
    metadata={
        "role": "extension_analyzer",
        EXTENSION_POLICY_METADATA_KEY: ExtensionPolicy().to_dict(),
    },
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        world_state=WorldStateReq(include_work_entities=True),
        work=WorkReq(current=True),
        events=EventsReq(recent=5),
        continuation=ContinuationReq(include=True),
    ),
)

RECONCILE_CAPABILITY_GAPS = ProcessDefinition(
    name="reconcile_capability_gaps",
    version="1",
    handler="reconcile_capability_gaps",
    trigger_event_types=(
        "capability_available",
        BACKEND_AVAILABLE,
        EXTENSION_ENVIRONMENT_CHANGED,
    ),
    metadata={"role": "extension_reconciler"},
    context_requirements=ContextRequirements(include_trigger_event=True),
)

#: Environment changes that justify looking at an open gap again (spec §70).
#: Not event replay: nothing is re-interpreted and no gap is re-derived — the
#: existing gap is simply analyzed against a world that has since changed.
REANALYSIS_EVENTS = (BACKEND_AVAILABLE, EXTENSION_ENVIRONMENT_CHANGED)


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError, TypeError):
        return None


# --- gap analysis -----------------------------------------------------------


async def analyze_capability_gap(ctx: ProcessContext) -> ProcessResult:
    """Work out what is missing and how it might be acquired — never acquire it."""
    if ctx.resume_point == "await_extension_review":
        return await _handle_review(ctx)

    assert ctx.event is not None and ctx.services is not None
    if ctx.event.type == CAPABILITY_GAP_REANALYSIS_REQUIRED:
        return await _reanalyze(ctx)
    return await _analyze_missing(ctx)


async def _analyze_missing(ctx: ProcessContext) -> ProcessResult:
    """Turn a ``capability_missing`` into a durable gap, then a proposal."""
    payload = ctx.event.payload
    requirement_id = _uuid(payload.get("work_requirement_id"))
    requirement = ctx.services.get_work_requirement(requirement_id)
    if requirement is None:
        return ctx.fail(f"work requirement {requirement_id} not found")
    if requirement.resolved or requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW}:
        # A late or redelivered block for work that has since been met.
        return ctx.complete(
            output={"analyzed": False, "reason": f"work is {requirement.status.value}"}
        )

    declared = _declared_missing(payload, requirement)
    if not declared:
        # Every capability exists; they are merely scattered.  That is a
        # composition problem for Phase 4B, not a deficiency of ours
        # (spec §10) — proposing an extension for it would be the system
        # offering to build what it already has.
        return ctx.complete(
            output={"analyzed": False, "reason": "no missing capability in this match"}
        )

    still_missing = _still_missing(ctx.services, declared)
    if not still_missing:
        # The registry moved between the block and this activation.  Recording
        # the gap as already closed is the truthful answer, and the one that
        # keeps unnecessary extensions from being proposed (spec §124).
        return _already_available(ctx, requirement, declared)

    gap, created = _find_or_open_gap(ctx, requirement, declared, still_missing)
    # The deficiency itself is announced once, when it is first recorded.  It
    # does not restate ``capability_missing`` (spec §49): that event says a
    # match failed, this one says a durable gap now exists and gives its id.
    opened = [_gap_event(ctx, CAPABILITY_GAP_OPENED, gap)] if created else []
    return await _propose_for_gap(
        ctx, gap, requirement, created=created, force=False, extra_events=opened
    )


async def _reanalyze(ctx: ProcessContext) -> ProcessResult:
    """Look at an open gap again because the environment changed (spec §69)."""
    payload = ctx.event.payload
    gap_id = _uuid(payload.get("capability_gap_id"))
    gap = ctx.services.get_capability_gap(gap_id)
    if gap is None:
        return ctx.fail(f"capability gap {gap_id} not found")
    if gap.status.terminal:
        return ctx.complete(
            output={"analyzed": False, "reason": f"gap is {gap.status.value}"}
        )

    requirement = ctx.services.get_work_requirement(gap.work_requirement_id)
    if not _still_missing(ctx.services, gap.missing_capabilities):
        ctx.update_capability_gap(gap.id, CapabilityGapStatus.RESOLVED)
        return ctx.complete(
            output={"analyzed": False, "resolved": True},
            emitted_events=[_gap_event(ctx, CAPABILITY_GAP_RESOLVED, gap)],
        )
    return await _propose_for_gap(ctx, gap, requirement, created=False, force=True)


async def _propose_for_gap(
    ctx: ProcessContext,
    gap,
    requirement,
    *,
    created: bool,
    force: bool,
    extra_events=(),
) -> ProcessResult:
    """Analyze the routes out of a gap and turn the best one into a proposal."""
    store = ctx.services.get_extension_store()
    existing = store.proposals_for_gap(gap.id) if store is not None and not created else []
    approved = [p for p in existing if p.status is ExtensionProposalStatus.APPROVED]
    live = [p for p in existing if p.status.live]

    if approved:
        # An approved proposal is already on its way to construction; quietly
        # replacing it would discard a decision somebody made (Invariant 92).
        _mark_gap(ctx, gap, CapabilityGapStatus.PROPOSAL_APPROVED, created=created)
        return ctx.complete(
            output={
                "analyzed": False,
                "reason": "an approved proposal is awaiting construction",
                "extension_proposal_id": str(approved[-1].id),
            },
            emitted_events=list(extra_events),
        )
    if live and not force:
        # Idempotency: a redelivered capability_missing must not produce a
        # second proposal for the same deficiency (spec §67).
        _mark_gap(ctx, gap, CapabilityGapStatus.PROPOSAL_AVAILABLE, created=created)
        return ctx.complete(
            output={
                "analyzed": False,
                "reason": "a proposal for this gap is already open",
                "extension_proposal_id": str(live[-1].id),
            },
            emitted_events=list(extra_events),
        )

    analyzer = ctx.services.get_acquisition_analyzer()
    if analyzer is None:
        return ctx.fail("the acquisition analyzer is not available")
    candidates = analyzer.analyze(gap, environment=ctx.services.get_acquisition_environment())
    usable = _usable(candidates)
    if not usable:
        return _acquisition_unavailable(
            ctx, gap, requirement, candidates, created=created, extra_events=extra_events
        )

    proposal, retry = await _build(ctx, gap, requirement, usable)
    if retry is not None:
        return retry

    duplicate = ctx.services.find_extension_proposal_by_fingerprint(proposal.fingerprint)
    if duplicate is not None:
        # The same analysis over the same registry: the same proposal, not a
        # second one (spec §68).  The gap goes back to describing where that
        # proposal actually stands, rather than staying mid-re-analysis.
        _mark_gap(
            ctx,
            gap,
            CapabilityGapStatus.PROPOSAL_AVAILABLE
            if duplicate.status.live
            else CapabilityGapStatus.OPEN,
            created=created,
        )
        return ctx.complete(
            output={
                "analyzed": True,
                "proposed": False,
                "reason": "this proposal was already made",
                "extension_proposal_id": str(duplicate.id),
            },
            emitted_events=list(extra_events),
        )

    for superseded in live:
        ctx.update_extension_proposal(
            superseded.id,
            ExtensionProposalStatus.SUPERSEDED,
            reasons=[f"superseded by {proposal.id} after re-analysis"],
        )
        for continuation in ctx.services.find_extension_review_continuations(
            superseded.id
        ):
            ctx.close_continuation(
                continuation.id,
                process_instance_id=continuation.process_instance_id,
                reason="extension proposal superseded",
            )
    return _decide(
        ctx, gap, proposal, usable, requirement, created=created, extra_events=extra_events
    )


async def _build(ctx: ProcessContext, gap, requirement, candidates):
    """Write the proposal — with a model if one is installed, without if not.

    Returns ``(proposal, retry_result)``.  The deterministic builder is the
    floor, not the fallback of last resort (spec §82): a backend that is down
    costs this system the wording of its proposal, never its ability to notice
    what it is missing and ask about it.
    """
    proposer = ctx.services.get_llm_extension_proposer()
    if proposer is None:
        return _deterministic(ctx, gap, requirement, candidates), None

    shortlist = proposer.shortlist(candidates) or candidates
    proposal, result, _request = await proposer.propose(
        gap,
        shortlist,
        work_requirement_id=gap.work_requirement_id,
        created_by_process_id=ctx.instance.id,
        context=ctx.view.to_snapshot_dict() if ctx.view else {},
        work_summary=_work_summary(requirement),
    )

    # Record the attempt before judging it: an invocation journal that only
    # kept successes would be silent about exactly the calls worth explaining.
    invocation = LLMInvocation(
        process_instance_id=ctx.instance.id,
        backend=BACKEND_NAME,
        activation_id=ctx.activation_id,
        model=getattr(result, "model", None),
        request_metadata={
            "capability_gap_id": str(gap.id),
            "candidates": len(shortlist),
        },
        response_metadata={
            "usage": getattr(result, "usage", None),
            "latency_ms": getattr(result, "latency_ms", None),
        },
        context_snapshot_id=ctx.context_snapshot_id,
        success=bool(getattr(result, "success", False)),
        error=getattr(result, "error", None),
    )
    ctx.record_llm_invocation(invocation)

    if not result.success or proposal is None:
        problem = (
            f"backend failure: {result.error}"
            if not result.success
            else "backend returned unparseable structured output"
        )
        if ctx.instance.retry_count < ctx.instance.max_retries:
            invocation.success = False
            invocation.error = invocation.error or problem
            return None, ctx.retry(f"extension proposal {problem}")
        ctx.logger.warning(
            "extension proposer unusable (%s); falling back to the deterministic "
            "builder for gap %s",
            problem,
            gap.id,
        )
        fallback = _deterministic(ctx, gap, requirement, candidates)
        fallback.reasons.append(
            f"llm proposer unavailable after {ctx.instance.retry_count + 1} "
            f"attempt(s) ({problem}); proposal derived deterministically"
        )
        return fallback, None

    proposal.llm_invocation_id = invocation.id
    proposal.context_snapshot_id = ctx.context_snapshot_id
    proposal.work_requirement_id = gap.work_requirement_id
    return proposal, None


def _deterministic(ctx: ProcessContext, gap, requirement, candidates) -> ExtensionProposal:
    """The derived proposal: the best-reusing candidate, described plainly."""
    return build_proposal(
        gap,
        candidates[0],
        candidates=candidates,
        work_requirement_id=gap.work_requirement_id,
        created_by_process_id=ctx.instance.id,
        context_snapshot_id=ctx.context_snapshot_id,
        source=SOURCE_DETERMINISTIC,
    )


def _decide(
    ctx: ProcessContext,
    gap,
    proposal: ExtensionProposal,
    candidates,
    requirement,
    *,
    created: bool,
    reviewed_by_event_id: uuid.UUID | None = None,
    human_approved: bool = False,
    announce: bool = True,
    extra_events=(),
) -> ProcessResult:
    """Validate, classify, and apply policy — the only route to APPROVED.

    Risk is recomputed from the proposal's own content whatever it claims about
    itself (spec §40): a model that rates its own extension LOW does not
    thereby make it low, and a proposal that under-declares its permissions is
    refused rather than believed.

    ``human_approved`` re-runs every *validation* stage but skips the risk gate
    — a person already answered that question, and only proposals policy sent
    to review can reach this path at all, so nothing CRITICAL arrives here
    (Invariant 91).  Re-validating on approval is the point: permissions,
    registrations and the registry may all have moved while the proposal sat
    waiting.
    """
    # A proposal is announced once, when it is first made.  Re-deciding an
    # existing one (a human approving after review) is not a second proposal.
    proposed = (
        [_proposal_event(ctx, EXTENSION_PROPOSED, proposal)] if announce else []
    )
    policy = _policy_for(ctx)
    validator = ctx.services.get_extension_validator()
    validation = validator.validate(
        proposal,
        gap=gap,
        candidates=candidates,
        policy=policy,
        definitions=ctx.services.get_all_definitions(),
    )

    proposal.estimated_risk = classify_risk(
        proposal.strategy,
        components=proposal.proposed_components,
        permissions=list(proposal.required_permissions) + validation.implied_permissions,
    )
    if human_approved:
        decision = (
            ExtensionDecision.APPROVE if validation.ok else ExtensionDecision.REJECT
        )
    else:
        decision = policy.decide(proposal.estimated_risk, valid=validation.ok)
    if (
        validation.requests_core_change
        and decision is ExtensionDecision.APPROVE
        and not human_approved
    ):
        # Never decided automatically, whatever the risk table says (spec §38).
        decision = ExtensionDecision.REVIEW
    proposal.human_approval_required = decision is not ExtensionDecision.APPROVE
    proposal.reasons.extend(validation.reasons)

    ctx.record_extension_decision(
        ExtensionDecisionRecord(
            extension_proposal_id=proposal.id,
            capability_gap_id=gap.id,
            decision=decision,
            estimated_risk=proposal.estimated_risk,
            decided_by_process_id=ctx.instance.id,
            validation_ok=validation.ok,
            reasons=list(validation.reasons),
            policy=policy.to_dict(),
            required_permissions=list(proposal.required_permissions),
            # Declared, never granted (spec §77): this column records what
            # construction *would* need, and Phase 5A grants none of it.
            granted_permissions=[],
            reviewed_by_event_id=reviewed_by_event_id,
        )
    )
    ctx.logger.info(
        "extension proposal %s for gap %s: %s (strategy=%s, risk=%s, reasons=%s)",
        proposal.id,
        gap.id,
        decision.value,
        proposal.declared_strategy,
        proposal.estimated_risk.value,
        validation.reasons,
    )

    if not validation.ok:
        proposal.status = ExtensionProposalStatus.INVALID
        ctx.record_extension_proposal(proposal)
        _mark_gap(ctx, gap, CapabilityGapStatus.OPEN, created=created)
        return ctx.complete(
            output={
                "proposed": True,
                "decision": decision.value,
                "valid": False,
                "extension_proposal_id": str(proposal.id),
                "reasons": list(validation.reasons),
            },
            emitted_events=[
                *extra_events,
                _proposal_event(
                    ctx,
                    EXTENSION_PROPOSAL_INVALID,
                    proposal,
                    reasons=list(validation.reasons),
                ),
            ],
        )

    if decision is ExtensionDecision.REJECT:
        proposal.status = ExtensionProposalStatus.REJECTED
        ctx.record_extension_proposal(proposal)
        _mark_gap(ctx, gap, CapabilityGapStatus.OPEN, created=created)
        return ctx.complete(
            output={
                "proposed": True,
                "decision": decision.value,
                "extension_proposal_id": str(proposal.id),
            },
            emitted_events=[
                *extra_events,
                *proposed,
                _proposal_event(
                    ctx, EXTENSION_REJECTED, proposal, reasons=list(validation.reasons)
                ),
            ],
        )

    if decision is ExtensionDecision.APPROVE:
        proposal.status = ExtensionProposalStatus.APPROVED
        ctx.record_extension_proposal(proposal)
        # PROPOSAL_APPROVED, never RESOLVED: we have decided how we would
        # acquire this, and we still cannot do it (spec §62).
        _mark_gap(ctx, gap, CapabilityGapStatus.PROPOSAL_APPROVED, created=created)
        return ctx.complete(
            output={
                "proposed": True,
                "decision": decision.value,
                "extension_proposal_id": str(proposal.id),
            },
            emitted_events=[
                *extra_events,
                *proposed,
                _proposal_event(ctx, EXTENSION_APPROVED, proposal),
            ],
        )

    proposal.status = ExtensionProposalStatus.REVIEW
    ctx.record_extension_proposal(proposal)
    _mark_gap(ctx, gap, CapabilityGapStatus.PROPOSAL_AVAILABLE, created=created)
    return ctx.suspend(
        resume_point="await_extension_review",
        waiting_for={
            "event_type": EXTENSION_REVIEWED,
            "proposal_id": str(proposal.id),
        },
        saved_process_state={
            "extension_proposal_id": str(proposal.id),
            "capability_gap_id": str(gap.id),
        },
        emitted_events=[
            *extra_events,
            *proposed,
            _proposal_event(
                ctx,
                EXTENSION_REVIEW_REQUIRED,
                proposal,
                reasons=list(validation.reasons),
            ),
        ],
    )


# --- human review -----------------------------------------------------------


async def _handle_review(ctx: ProcessContext) -> ProcessResult:
    """Apply a person's decision about a proposed self-extension (spec §45)."""
    assert ctx.event is not None and ctx.services is not None
    payload = ctx.event.payload
    proposal_id = _uuid(payload.get("proposal_id")) or _uuid(
        ctx.saved_process_state.get("extension_proposal_id")
    )
    human_decision = str(payload.get("decision", "reject")).lower()

    proposal = ctx.services.get_extension_proposal(proposal_id)
    if proposal is None:
        return ctx.fail(f"extension proposal {proposal_id} not found on review")
    if proposal.status is ExtensionProposalStatus.SUPERSEDED:
        return ctx.complete(
            output={"decided": False, "reason": "the proposal was superseded"}
        )
    if proposal.status is not ExtensionProposalStatus.REVIEW:
        # Exactly one decision converges even if the review event arrives twice
        # or a restart replayed the resume (spec §84).
        return ctx.complete(
            output={
                "decided": False,
                "reason": f"proposal is already {proposal.status.value}",
            }
        )

    gap = ctx.services.get_capability_gap(proposal.capability_gap_id)
    if gap is None:
        return ctx.fail("the capability gap disappeared during review")
    requirement = ctx.services.get_work_requirement(gap.work_requirement_id)

    if human_decision == "modify":
        return _handle_modify(ctx, gap, proposal, requirement, payload.get("replacement"))

    if human_decision == "approve":
        # Never trust the earlier verdict: re-validate against the world as it
        # is now, not as it was when the proposal was written.  A capability
        # registered during the review makes this extension unnecessary, and a
        # route that has since disappeared makes it unbuildable.
        analyzer = ctx.services.get_acquisition_analyzer()
        candidates = _usable(
            analyzer.analyze(gap, environment=ctx.services.get_acquisition_environment())
        )
        return _decide(
            ctx,
            gap,
            proposal,
            candidates,
            requirement,
            created=False,
            reviewed_by_event_id=ctx.event.id,
            human_approved=True,
            announce=False,
        )

    ctx.update_extension_proposal(
        proposal.id,
        ExtensionProposalStatus.REJECTED,
        reasons=["rejected by human review"],
    )
    # The gap goes back to OPEN, not RESOLVED and not CANCELLED: refusing one
    # way of acquiring a capability says nothing about still lacking it.
    ctx.update_capability_gap(gap.id, CapabilityGapStatus.OPEN)
    ctx.record_extension_decision(
        ExtensionDecisionRecord(
            extension_proposal_id=proposal.id,
            capability_gap_id=gap.id,
            decision=ExtensionDecision.REJECT,
            estimated_risk=proposal.estimated_risk,
            decided_by_process_id=ctx.instance.id,
            reasons=["rejected by human review"],
            required_permissions=list(proposal.required_permissions),
            reviewed_by_event_id=ctx.event.id,
        )
    )
    return ctx.complete(
        output={
            "decided": True,
            "decision": "REJECT",
            "extension_proposal_id": str(proposal.id),
        },
        emitted_events=[
            _proposal_event(
                ctx, EXTENSION_REJECTED, proposal, reasons=["rejected by human review"]
            )
        ],
    )


def _handle_modify(ctx, gap, proposal, requirement, replacement) -> ProcessResult:
    """Replace a reviewed proposal with a human-supplied one (spec §46).

    The replacement is a *new* proposal that re-enters validation from the top;
    a human edit never yields a directly-approved extension.  The original is
    kept and marked SUPERSEDED — its history is not rewritten (Invariant 92).
    """
    new_proposal = ExtensionProposal.from_output(
        replacement,
        capability_gap_id=gap.id,
        work_requirement_id=gap.work_requirement_id,
        target_capabilities=list(gap.missing_capabilities),
        candidate_strategies=list(proposal.candidate_strategies),
        created_by_process_id=ctx.instance.id,
        source=SOURCE_HUMAN,
        root_proposal_id=proposal.root_proposal_id,
        replaces_proposal_id=proposal.id,
    )
    if new_proposal is None:
        return ctx.fail("modify replacement is not a valid extension proposal")
    new_proposal.context_snapshot_id = ctx.context_snapshot_id
    new_proposal.analysis = dict(proposal.analysis)

    ctx.update_extension_proposal(
        proposal.id,
        ExtensionProposalStatus.SUPERSEDED,
        reasons=[f"superseded by {new_proposal.id} after human modification"],
    )
    ctx.record_extension_decision(
        ExtensionDecisionRecord(
            extension_proposal_id=proposal.id,
            capability_gap_id=gap.id,
            decision=ExtensionDecision.REJECT,
            estimated_risk=proposal.estimated_risk,
            decided_by_process_id=ctx.instance.id,
            reasons=[f"superseded by {new_proposal.id}"],
            reviewed_by_event_id=ctx.event.id,
        )
    )

    analyzer = ctx.services.get_acquisition_analyzer()
    candidates = _usable(
        analyzer.analyze(gap, environment=ctx.services.get_acquisition_environment())
    )
    return _decide(
        ctx,
        gap,
        new_proposal,
        candidates,
        requirement,
        created=False,
        reviewed_by_event_id=ctx.event.id,
    )


# --- reconciliation ---------------------------------------------------------


async def reconcile_capability_gaps(ctx: ProcessContext) -> ProcessResult:
    """Close gaps the world closed for us, and re-open the question for the rest.

    Two different reactions to "something changed" (spec §61, §69):

    * a gap whose missing capabilities are all provided now is RESOLVED —
      however that happened, and specifically *without* any extension having
      been constructed;
    * a gap that still stands is analyzed again against the changed
      environment, which may find a reuse route that did not exist before.

    Neither is event replay (Invariant 52 applied to gaps): no raw event is
    re-delivered and no gap is re-derived — the existing rows, with their own
    ids and provenance, are simply looked at again.
    """
    assert ctx.event is not None and ctx.services is not None
    capability_name = ctx.event.payload.get("capability_name")
    resolved: list[str] = []
    reanalyzed: list[str] = []
    emitted = []
    for gap in ctx.services.get_open_capability_gaps():
        if not _still_missing(ctx.services, gap.missing_capabilities):
            ctx.update_capability_gap(gap.id, CapabilityGapStatus.RESOLVED)
            emitted.append(_gap_event(ctx, CAPABILITY_GAP_RESOLVED, gap))
            resolved.append(str(gap.id))
            continue
        if not _affects(gap, ctx.event.type, capability_name):
            continue
        if gap.status is CapabilityGapStatus.PROPOSAL_PENDING:
            continue  # a re-analysis is already outstanding for this gap
        ctx.update_capability_gap(gap.id, CapabilityGapStatus.PROPOSAL_PENDING)
        emitted.append(
            ctx.new_event(
                CAPABILITY_GAP_REANALYSIS_REQUIRED,
                {
                    "capability_gap_id": str(gap.id),
                    "work_requirement_id": str(gap.work_requirement_id),
                    "reason": f"{ctx.event.type} changed what is available",
                },
            )
        )
        reanalyzed.append(str(gap.id))

    if resolved or reanalyzed:
        ctx.logger.info(
            "gap reconciliation after %s: %d resolved, %d re-analysed",
            ctx.event.type,
            len(resolved),
            len(reanalyzed),
        )
    return ctx.complete(
        output={"resolved": resolved, "reanalysis_requested": reanalyzed},
        emitted_events=emitted,
    )


def _affects(gap, event_type: str, capability_name: str | None) -> bool:
    """Whether this change is a reason to look at this gap again."""
    if event_type in REANALYSIS_EVENTS:
        return True
    if capability_name is None:
        return False
    return capability_name in gap.missing_names


# --- shared helpers ---------------------------------------------------------


def _declared_missing(payload, requirement) -> list[CapabilityRequirement]:
    """What the match said was missing, as full requirements.

    Names come from the event, but the *requirement objects* come from the work
    — they carry the acquisition hints, and re-deriving them from bare names
    would throw away everything that makes a useful analysis possible.
    """
    names = list(payload.get("missing_capabilities") or [])
    if not names:
        names = list(requirement.missing_capabilities or [])
    known = {r.name: r for r in requirement.required_capabilities}
    return [known.get(name) or CapabilityRequirement(name=name) for name in names]


def _still_missing(services, requirements) -> list[CapabilityRequirement]:
    """The subset of ``requirements`` nothing currently usable provides.

    Asked through the matcher rather than the registry, because the two answer
    different questions (spec §71): a capability whose only provider is a
    *disabled* definition has a provider on paper and none in practice, and it
    is the practical answer that decides whether work is blocked.
    """
    matcher = services.get_capability_matcher() if services is not None else None
    if matcher is None:
        registry = services.get_capability_registry() if services is not None else None
        if registry is None:
            return list(requirements)
        return [r for r in requirements if not registry.is_provided(r)]
    definitions = services.get_all_definitions()
    return [r for r in requirements if not matcher.provides(r, definitions)]


def _usable(candidates) -> list:
    """The candidate routes this architecture can actually express (spec §65)."""
    return [
        c
        for c in candidates
        if c.strategy is not ExtensionStrategy.UNSUPPORTED
        and c.feasibility is not AcquisitionFeasibility.UNSUPPORTED
    ]


def _find_or_open_gap(ctx, requirement, declared, missing):
    """Return ``(gap, created)`` for this need and this exact missing set.

    Dedup is by the *logical* key, not by id (spec §11): two derivations of the
    same deficiency are one gap, so a redelivered event finds the first.
    """
    key = missing_key_for(missing)
    existing = ctx.services.find_capability_gap(requirement.id, key)
    if existing is not None:
        return existing, False

    matches = ctx.services.get_capability_matches(requirement.id)
    gap = CapabilityGap(
        work_requirement_id=requirement.id,
        required_capabilities=list(requirement.required_capabilities),
        missing_capabilities=list(missing),
        current_partial_providers=_partial_providers(ctx, requirement, missing),
        reason=(
            f"{requirement.work_key} needs {[r.name for r in missing]}, "
            "which nothing currently provides"
        ),
        source_match_id=matches[-1].id if matches else None,
    )
    ctx.open_capability_gap(gap)
    ctx.logger.info(
        "capability gap opened for %s: missing %s",
        requirement.work_key,
        gap.missing_names,
    )
    return gap, True


def _partial_providers(ctx, requirement, missing) -> list[str]:
    """Definitions that cover *some* of what this work needs.

    The interesting reuse question is rarely "who can do this?" — it is "who is
    nearly right?", and the answer is worth keeping beside the gap.
    """
    matcher = ctx.services.get_capability_matcher()
    if matcher is None:
        return []
    result = matcher.match(
        list(requirement.required_capabilities), ctx.services.get_all_definitions()
    )
    return sorted(
        f"{c.definition_name}:{c.definition_version}"
        for c in result.candidates
        if c.covered_capabilities
    )


def _mark_gap(ctx, gap, status, *, created: bool) -> None:
    """Set a gap's status, whether it is being created or already stored.

    A gap staged in this same activation is not in the database yet, so its
    status is set on the object; an existing one gets a staged update.  Doing
    both would be two writes to one record in one activation, which the effect
    checker refuses (Invariant 65).
    """
    if created:
        gap.status = status
    else:
        ctx.update_capability_gap(gap.id, status)


def _already_available(ctx, requirement, declared) -> ProcessResult:
    """Everything said to be missing is provided again — close any open gap."""
    key = missing_key_for(declared)
    gap = ctx.services.find_capability_gap(requirement.id, key)
    events = []
    if gap is not None and not gap.status.terminal:
        ctx.update_capability_gap(gap.id, CapabilityGapStatus.RESOLVED)
        events.append(_gap_event(ctx, CAPABILITY_GAP_RESOLVED, gap))
    return ctx.complete(
        output={
            "analyzed": False,
            "reason": "every missing capability is provided again",
            "capability_gap_id": str(gap.id) if gap else None,
        },
        emitted_events=events,
    )


def _acquisition_unavailable(
    ctx, gap, requirement, candidates, *, created: bool, extra_events=()
):
    """No route this architecture can express — say so, keep the gap (spec §66).

    The need is not cancelled and the gap is not closed.  "We cannot currently
    see a way to acquire this" is a truthful, useful answer; inventing a
    plausible proposal to avoid saying it would not be.
    """
    reasons = [r for c in candidates for r in c.reasons] or [
        "no acquisition strategy is available for this gap"
    ]
    _mark_gap(ctx, gap, CapabilityGapStatus.OPEN, created=created)
    ctx.logger.info("no acquisition route for gap %s: %s", gap.id, reasons)
    return ctx.complete(
        output={"analyzed": True, "proposed": False, "reasons": reasons},
        emitted_events=[
            *extra_events,
            ctx.new_event(
                CAPABILITY_ACQUISITION_UNAVAILABLE,
                {
                    "capability_gap_id": str(gap.id),
                    "work_requirement_id": str(gap.work_requirement_id),
                    "missing_capabilities": gap.missing_names,
                    "reasons": reasons,
                },
            ),
        ],
    )


def _policy_for(ctx: ProcessContext) -> ExtensionPolicy:
    """Read the extension policy off this process's own definition metadata.

    Policy is configuration, so it travels with the definition in SQLite rather
    than being compiled into the handler or the Runtime (spec §42).
    """
    if ctx.services is None:
        return ExtensionPolicy()
    definition = ctx.services.get_definition(
        ctx.instance.definition_name, ctx.instance.definition_version
    )
    metadata = definition.metadata if definition else {}
    return ExtensionPolicy.from_dict(metadata.get(EXTENSION_POLICY_METADATA_KEY))


def _work_summary(requirement) -> dict:
    if requirement is None:
        return {}
    return {
        "work_type": requirement.work_type,
        "work_key": requirement.work_key,
        "reason": requirement.reason,
    }


def _gap_event(ctx, event_type: str, gap):
    return ctx.new_event(
        event_type,
        {
            "capability_gap_id": str(gap.id),
            "work_requirement_id": str(gap.work_requirement_id),
            "missing_capabilities": gap.missing_names,
        },
    )


def _proposal_event(ctx, event_type: str, proposal, **extra):
    payload = {
        "extension_proposal_id": str(proposal.id),
        "proposal_id": str(proposal.id),
        "root_proposal_id": str(proposal.root_proposal_id),
        "capability_gap_id": str(proposal.capability_gap_id),
        "strategy": proposal.declared_strategy,
        "estimated_risk": proposal.estimated_risk.value,
        "target_capabilities": proposal.target_names,
    }
    payload.update(extra)
    return ctx.new_event(event_type, payload)


# --- wiring -----------------------------------------------------------------


def bootstrap_extension(runtime, *, policy: ExtensionPolicy | None = None) -> None:
    """Register the self-extension pipeline.

    ``policy`` is persisted on the analyzer's definition, so a runtime rebuilt
    from the same database keeps the same appetite for self-extension — with no
    ``policy`` given, an appetite already recorded there is kept rather than
    quietly reset to the default by the act of restarting.
    """
    analyzer = ANALYZE_CAPABILITY_GAP
    if policy is None:
        stored = runtime.process_store.get_definition(
            ANALYZE_CAPABILITY_GAP.name, ANALYZE_CAPABILITY_GAP.version
        )
        recorded = (stored.metadata or {}).get(EXTENSION_POLICY_METADATA_KEY) if stored else None
        if recorded is not None:
            policy = ExtensionPolicy.from_dict(recorded)
    if policy is not None:
        runtime.set_extension_policy(policy)
        analyzer = ProcessDefinition(
            name=ANALYZE_CAPABILITY_GAP.name,
            version=ANALYZE_CAPABILITY_GAP.version,
            handler=ANALYZE_CAPABILITY_GAP.handler,
            trigger_event_types=ANALYZE_CAPABILITY_GAP.trigger_event_types,
            max_retries=ANALYZE_CAPABILITY_GAP.max_retries,
            metadata={
                **ANALYZE_CAPABILITY_GAP.metadata,
                EXTENSION_POLICY_METADATA_KEY: policy.to_dict(),
            },
            context_requirements=ANALYZE_CAPABILITY_GAP.context_requirements,
        )
    runtime.register_process(analyzer, analyze_capability_gap)
    runtime.register_process(RECONCILE_CAPABILITY_GAPS, reconcile_capability_gaps)


__all__ = [
    "ANALYZE_CAPABILITY_GAP",
    "BACKEND_AVAILABLE",
    "CAPABILITY_ACQUISITION_UNAVAILABLE",
    "CAPABILITY_GAP_OPENED",
    "CAPABILITY_GAP_REANALYSIS_REQUIRED",
    "CAPABILITY_GAP_RESOLVED",
    "EXTENSION_APPROVED",
    "EXTENSION_ENVIRONMENT_CHANGED",
    "EXTENSION_POLICY_METADATA_KEY",
    "EXTENSION_PROPOSAL_INVALID",
    "EXTENSION_PROPOSED",
    "EXTENSION_REJECTED",
    "EXTENSION_REVIEWED",
    "EXTENSION_REVIEW_REQUIRED",
    "RECONCILE_CAPABILITY_GAPS",
    "analyze_capability_gap",
    "bootstrap_extension",
    "reconcile_capability_gaps",
]
