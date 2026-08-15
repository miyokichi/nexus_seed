"""Plan selection and replanning, as ordinary Processes.

    plan_candidates_ready -> select_process_plan -> the chosen plan runs
    replan_required       -> replan_work         -> another set of candidates

No Runtime feature was added for either (spec §75).  Selection suspends on the
ordinary Continuation when a human has to look at it, replanning is triggered
by an ordinary durable event, and both commit through the ordinary atomic
transition — so restart-safety, idempotency and bounded drain apply to a
decision exactly as they do to anything else.

The shape of ``select_process_plan``::

    candidates
      -> evaluations (already recorded at composition)
      -> hard constraints          eligibility, not preference
      -> deterministic selection   always available
      -> LLM proposal              optional, only over what remains
      -> validation                against the world as it is now
      -> policy                    accept / review / fall back
      -> PlanSelection + plan VALIDATED + process_plan_created

Every arrow in that list can only narrow.  There is no step at which something
that was excluded comes back, which is what makes a hard constraint hard
(Invariant 76).
"""

from __future__ import annotations

import logging
import uuid

from ..backends.base import BackendRequest, LLMInvocation
from ..context.requirements import (
    ContextRequirements,
    ContinuationReq,
    EventsReq,
    WorkReq,
    WorldStateReq,
)
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..decision.models import (
    DecisionPreference,
    PlanSelection,
    PlanSelectionProposal,
    ReplanAttempt,
    SelectionMethod,
    SelectionProposalStatus,
)
from ..decision.policy import SelectionDecision, eligible
from ..decision.selector import DeterministicPlanSelector
from ..planning.models import PlanStatus
from ..processes.planning import PLAN_CANDIDATES_READY, REPLAN_REQUIRED
from ..work.work_requirement import WorkStatus

logger = logging.getLogger("nexus_seed.process.decision")

#: The name a handler looks up in ``ctx.backends`` for an LLM selector.
BACKEND_NAME = "llm"

#: A human was asked which plan to run.
PLAN_SELECTION_REVIEWED = "plan_selection_reviewed"

#: A plan was chosen and is about to run.
PLAN_SELECTED = "plan_selected"

#: The need stands but no plan we can build satisfies it.
NO_VALID_PLAN = "no_valid_plan"
REPLAN_UNAVAILABLE = "replan_unavailable"


SELECT_PROCESS_PLAN = ProcessDefinition(
    name="select_process_plan",
    version="1",
    handler="select_process_plan",
    trigger_event_types=(PLAN_CANDIDATES_READY,),
    max_retries=2,
    metadata={"role": "decision"},
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        world_state=WorldStateReq(include_work_entities=True),
        continuation=ContinuationReq(include=True),
        events=EventsReq(recent=5),
        work=WorkReq(current=True),
    ),
)

REPLAN_WORK = ProcessDefinition(
    name="replan_work",
    version="1",
    handler="replan_work",
    trigger_event_types=(REPLAN_REQUIRED,),
    metadata={"role": "decision"},
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        world_state=WorldStateReq(include_work_entities=True),
        events=EventsReq(recent=10),
        work=WorkReq(current=True),
    ),
)


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError, TypeError):
        return None


# --- selection --------------------------------------------------------------


async def select_process_plan(ctx: ProcessContext) -> ProcessResult:
    """Choose which of the composed candidates to run."""
    if ctx.resume_point == "await_review":
        return _handle_review(ctx)
    return await _select_fresh(ctx)


async def _select_fresh(ctx: ProcessContext) -> ProcessResult:
    assert ctx.event is not None and ctx.services is not None
    requirement_id = _uuid(ctx.event.payload.get("work_requirement_id"))
    requirement = ctx.services.get_work_requirement(requirement_id)
    if requirement is None:
        return ctx.fail(f"work requirement {requirement_id} not found")
    if requirement.resolved or requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW}:
        return ctx.complete(
            output={"selected": False, "reason": f"work is {requirement.status.value}"}
        )

    # A need already pursuing a plan is not re-decided.  This is what makes a
    # redelivered plan_candidates_ready a no-op rather than a second decision
    # (spec §92), and it is also what enforces "at most one active plan".
    active = ctx.services.get_active_plan(requirement.id)
    if active is not None:
        return ctx.complete(
            output={"selected": False, "reason": "a plan is already active",
                    "plan_id": str(active.id)}
        )

    context = _decision_context(ctx, requirement)
    if context is None:
        return ctx.fail("the decision layer is not available")
    if not context["evaluations"]:
        return _no_choice(ctx, requirement, ["no candidate plans to choose from"])

    allowed, rejected = eligible(context["evaluations"], context["preference"])
    if not allowed:
        # Every candidate was ruled out by a hard constraint.  Not a failure of
        # the planner and not a reason to lower the bar (Invariant 76).
        return _no_choice(
            ctx,
            requirement,
            [r for check in rejected for r in check.violations],
            rejected_plan_ids=[c.plan_id for c in rejected],
        )

    selector = ctx.services.get_llm_plan_selector()
    if selector is None or len(allowed) == 1:
        # One eligible option is not a judgement call, so no model is asked
        # (spec §80 in spirit: nothing to choose between).
        return _decide_deterministically(ctx, requirement, context, allowed, rejected)
    return await _decide_with_llm(ctx, requirement, context, allowed, rejected, selector)


def _decide_deterministically(
    ctx, requirement, context, allowed, rejected, *, proposal=None, extra_reasons=()
) -> ProcessResult:
    """Pick by weighted score — the path that always exists (Invariant 77)."""
    selector = ctx.services.get_plan_selector() or DeterministicPlanSelector()
    scored = selector.select(allowed, context["preference"])
    if scored is None:
        return _no_choice(ctx, requirement, ["nothing to select from"])

    reasons = list(extra_reasons)
    reasons.append(
        f"deterministic: score {scored.score:.4f} over {len(allowed)} eligible plan(s)"
    )
    reasons.extend(_rejection_reasons(rejected))

    if _approval_required(scored.evaluation, context["preference"]):
        review_proposal = PlanSelectionProposal(
            work_requirement_id=requirement.id,
            candidate_plan_ids=[e.plan_id for e in allowed],
            selected_plan_id=scored.plan_id,
            confidence=1.0,
            rationale="deterministic selection requires human approval",
            created_by_process_id=ctx.instance.id,
            context_snapshot_id=ctx.context_snapshot_id,
            status=SelectionProposalStatus.REVIEW,
            reasons=list(reasons),
        )
        ctx.record_selection_proposal(review_proposal)
        return _suspend_for_review(ctx, requirement, review_proposal)

    return _commit_selection(
        ctx,
        requirement,
        context,
        plan_id=scored.plan_id,
        # DETERMINISTIC even when a proposal exists: this path is reached
        # precisely because the model's answer was not acted on, and recording
        # it as an LLM decision would misattribute the choice.
        method=SelectionMethod.DETERMINISTIC,
        score=scored.score,
        allowed=allowed,
        rejected=rejected,
        proposal=proposal,
        reasons=reasons,
    )


async def _decide_with_llm(
    ctx, requirement, context, allowed, rejected, selector
) -> ProcessResult:
    """Ask a model which eligible plan to run; treat the answer as a proposal."""
    shortlist = selector.shortlist(allowed, context["preference"])
    proposal, result, request = await selector.propose(
        shortlist,
        context["preference"],
        work_requirement_id=requirement.id,
        context=ctx.view.to_snapshot_dict() if ctx.view else {},
        work_summary={
            "work_type": requirement.work_type,
            "work_key": requirement.work_key,
            "reason": requirement.reason,
        },
        created_by_process_id=ctx.instance.id,
    )

    # Record the attempt before judging it: an invocation journal that only
    # kept successes would be silent about exactly the calls worth explaining.
    invocation = LLMInvocation(
        process_instance_id=ctx.instance.id,
        backend=BACKEND_NAME,
        activation_id=ctx.activation_id,
        model=result.model,
        request_metadata={"candidates": len(shortlist)},
        response_metadata={"usage": result.usage, "latency_ms": result.latency_ms},
        context_snapshot_id=ctx.context_snapshot_id,
        success=result.success,
        error=result.error,
    )
    ctx.record_llm_invocation(invocation)

    if not result.success:
        # A backend failure is transient; retry, and if the retries run out the
        # deterministic path still gets there (spec §81, §117).
        if ctx.instance.retry_count < ctx.instance.max_retries:
            return ctx.retry(f"selection backend failure: {result.error}")
        return _decide_deterministically(
            ctx,
            requirement,
            context,
            allowed,
            rejected,
            extra_reasons=[
                f"selection backend unavailable after "
                f"{ctx.instance.retry_count + 1} attempt(s): {result.error}"
            ],
        )
    if proposal is None:
        invocation.success = False
        invocation.error = "unparseable structured output"
        if ctx.instance.retry_count < ctx.instance.max_retries:
            return ctx.retry("selection backend returned unparseable structured output")
        return _decide_deterministically(
            ctx,
            requirement,
            context,
            allowed,
            rejected,
            extra_reasons=[
                "selection backend returned invalid structured output; "
                "retry budget exhausted"
            ],
        )

    proposal.llm_invocation_id = invocation.id
    proposal.context_snapshot_id = ctx.context_snapshot_id

    # Validated against the *shortlist*, not everything eligible: the model may
    # only choose from what it was shown, and a plan it could not have seen is
    # not a choice it made (spec §28).
    validation = _validate_selection(ctx, requirement, context, proposal, shortlist)
    policy = ctx.services.get_selection_policy()
    decision = policy.decide(proposal.confidence, valid=validation.ok)

    if not validation.ok:
        proposal.status = SelectionProposalStatus.INVALID
        proposal.reasons.extend(validation.reasons)
        ctx.record_selection_proposal(proposal)
        ctx.logger.warning(
            "selection proposal %s rejected: %s", proposal.id, validation.reasons
        )
        # A semantic invalidity (hallucinated/out-of-set/drifted plan) is not
        # the same as low confidence or an unavailable backend.  Nothing runs
        # on this decision attempt (spec §83–§85).
        return _no_choice(ctx, requirement, validation.reasons)

    chosen_evaluation = context["evaluation_map"].get(proposal.selected_plan_id)
    if _approval_required(chosen_evaluation, context["preference"]):
        decision = SelectionDecision.REVIEW

    if decision is SelectionDecision.ACCEPT:
        proposal.status = SelectionProposalStatus.ACCEPTED
        ctx.record_selection_proposal(proposal)
        return _commit_selection(
            ctx, requirement, context,
            plan_id=proposal.selected_plan_id,
            method=SelectionMethod.LLM,
            score=None,
            allowed=shortlist,
            rejected=rejected,
            proposal=proposal,
            reasons=[
                f"llm selected with confidence {proposal.confidence:.2f}",
                *_rejection_reasons(rejected),
            ],
        )

    if decision is SelectionDecision.REVIEW:
        proposal.status = SelectionProposalStatus.REVIEW
        ctx.record_selection_proposal(proposal)
        ctx.logger.info(
            "selection proposal %s -> REVIEW (confidence %.2f)",
            proposal.id,
            proposal.confidence,
        )
        return _suspend_for_review(ctx, requirement, proposal)

    # Too unsure to act on — but the need does not stop (spec §32).
    proposal.status = SelectionProposalStatus.REJECTED
    ctx.record_selection_proposal(proposal)
    if decision is SelectionDecision.FALLBACK:
        return _decide_deterministically(
            ctx, requirement, context, allowed, rejected,
            extra_reasons=[
                f"llm confidence {proposal.confidence:.2f} below review threshold"
            ],
        )
    return _no_choice(ctx, requirement, ["llm confidence too low and no fallback"])


def _handle_review(ctx: ProcessContext) -> ProcessResult:
    """A human said what to do with the proposed selection (spec §34)."""
    assert ctx.event is not None and ctx.services is not None
    payload = ctx.event.payload
    proposal_id = _uuid(payload.get("proposal_id"))
    decision = payload.get("decision", "reject")

    decisions = ctx.services.get_decision_store()
    proposal = decisions.get_proposal(proposal_id) if decisions else None
    if proposal is None:
        return ctx.fail(f"selection proposal {proposal_id} not found on review")

    requirement = ctx.services.get_work_requirement(proposal.work_requirement_id)
    if requirement is None:
        return ctx.fail("work requirement disappeared during review")
    if requirement.resolved or requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW}:
        return ctx.complete(output={"selected": False, "reason": "work already resolved"})

    context = _decision_context(ctx, requirement)
    if context is None:
        return ctx.fail("the decision layer is not available")
    allowed, rejected = eligible(context["evaluations"], context["preference"])

    if decision == "reject":
        ctx.update_selection_proposal(
            proposal_id, SelectionProposalStatus.REJECTED, reasons=["rejected by human"]
        )
        ctx.mark_work(requirement.id, WorkStatus.BLOCKED_PLAN)
        return ctx.complete(
            output={"selected": False, "decision": "reject", "proposal_id": str(proposal_id)},
            emitted_events=[
                ctx.new_event(
                    "plan_selection_rejected",
                    {
                        "proposal_id": str(proposal_id),
                        "work_requirement_id": str(requirement.id),
                    },
                )
            ],
        )

    chosen = proposal.selected_plan_id
    if decision == "choose_alternative":
        # A human may pick a different candidate, but not a plan that is not
        # one (spec §35).  The same rule as for the model, for the same reason.
        chosen = _uuid(payload.get("selected_plan_id"))

    revalidation = _validate_selection(
        ctx, requirement, context, proposal, allowed, selected_plan_id=chosen
    )
    if not revalidation.ok:
        # The world moved while a human was thinking (spec §30).
        ctx.update_selection_proposal(
            proposal_id, SelectionProposalStatus.INVALID, reasons=revalidation.reasons
        )
        return _no_choice(ctx, requirement, revalidation.reasons)

    ctx.update_selection_proposal(proposal_id, SelectionProposalStatus.ACCEPTED)
    return _commit_selection(
        ctx, requirement, context,
        plan_id=chosen,
        method=SelectionMethod.HUMAN,
        score=None,
        allowed=allowed,
        rejected=rejected,
        proposal=proposal,
        reasons=[f"human {decision}", *_rejection_reasons(rejected)],
    )


# --- the commit -------------------------------------------------------------


def _commit_selection(
    ctx, requirement, context, *, plan_id, method, score, allowed, rejected,
    proposal, reasons,
) -> ProcessResult:
    """Record the decision and start the chosen plan, in one transaction.

    Everything that makes the choice real lands together (spec §72): the
    selection record, the plan's promotion to VALIDATED, the demotion of the
    candidates not taken, the pointer from the need, and the event that starts
    execution.  A crash here leaves either all of it or none.
    """
    plans = ctx.services.get_plan_store()
    considered = [e.plan_id for e in allowed]
    not_taken = [pid for pid in context["candidate_ids"] if pid != plan_id]

    ctx.update_plan(plan_id, PlanStatus.VALIDATED)
    for other in not_taken:
        # Kept, not deleted: the options not taken are half of what makes a
        # decision reviewable afterwards (spec §41).
        ctx.update_plan(other, PlanStatus.SUPERSEDED)

    selection = PlanSelection(
        work_requirement_id=requirement.id,
        selected_plan_id=plan_id,
        selection_method=method,
        deterministic_score=score,
        selection_proposal_id=proposal.id if proposal is not None else None,
        replan_attempt=context["replan_attempt"],
        considered_plan_ids=considered,
        rejected_plan_ids=[c.plan_id for c in rejected],
        decision_reasons=list(reasons),
    )
    ctx.record_plan_selection(selection)
    ctx.mark_work(requirement.id, WorkStatus.PLANNED)

    nodes = plans.nodes(plan_id) if plans else []
    ctx.logger.info(
        "selected plan %s for %s by %s", plan_id, requirement.work_key, method.value
    )
    return ctx.complete(
        output={
            "selected": True,
            "plan_id": str(plan_id),
            "method": method.value,
            "considered": len(considered),
        },
        emitted_events=[
            ctx.new_event(
                PLAN_SELECTED,
                {
                    "plan_id": str(plan_id),
                    "work_requirement_id": str(requirement.id),
                    "method": method.value,
                    "selection_id": str(selection.id),
                },
            ),
            ctx.new_event(
                "process_plan_created",
                {
                    "plan_id": str(plan_id),
                    "work_requirement_id": str(requirement.id),
                    "node_count": len(nodes),
                },
            ),
        ],
    )


def _no_choice(ctx, requirement, reasons, *, rejected_plan_ids=()) -> ProcessResult:
    """No plan may be run — recorded as a decision, not as an error.

    The need is *not* cancelled (Invariant 79).  BLOCKED_PLAN says exactly what
    is true: the competence exists, and none of the arrangements we can
    currently make satisfies this need under its constraints.
    """
    selection = PlanSelection(
        work_requirement_id=requirement.id,
        selected_plan_id=None,
        selection_method=SelectionMethod.DETERMINISTIC,
        rejected_plan_ids=list(rejected_plan_ids),
        decision_reasons=list(reasons),
    )
    ctx.record_plan_selection(selection)
    ctx.mark_work(requirement.id, WorkStatus.BLOCKED_PLAN)
    ctx.logger.info("no plan selected for %s: %s", requirement.work_key, reasons)
    return ctx.complete(
        output={"selected": False, "reasons": list(reasons)},
        emitted_events=[
            ctx.new_event(
                NO_VALID_PLAN,
                {"work_requirement_id": str(requirement.id), "reasons": list(reasons)},
            )
        ],
    )


# --- shared helpers ---------------------------------------------------------


def _decision_context(ctx, requirement) -> dict | None:
    """Everything a decision needs, read once (spec §59).

    Deliberately narrow: the need, the preference, the candidates and their
    evaluations.  Not the database — a decision that could see everything would
    be a decision nobody could reproduce.
    """
    services = ctx.services
    plans = services.get_plan_store()
    decisions = services.get_decision_store()
    if plans is None or decisions is None:
        return None

    candidates = [p for p in services.get_plan_candidates(requirement.id)]
    evaluations = []
    graphs = {}
    plan_map = {}
    for plan in candidates:
        plan_map[plan.id] = plan
        graphs[plan.id] = (plans.nodes(plan.id), plans.edges(plan.id))
        evaluation = decisions.get_evaluation(plan.id)
        if evaluation is not None:
            evaluations.append(evaluation)

    return {
        "preference": requirement.decision_preference or DecisionPreference(),
        "candidate_ids": [p.id for p in candidates],
        "plans": plan_map,
        "graphs": graphs,
        "evaluations": evaluations,
        "evaluation_map": {e.plan_id: e for e in evaluations},
        "replan_attempt": max((p.replan_attempt for p in candidates), default=0),
    }


def _validate_selection(
    ctx, requirement, context, proposal, allowed, *, selected_plan_id=None
):
    """Re-check a chosen plan against the world as it is now (spec §29–§30)."""
    validator = ctx.services.get_selection_validator()
    chosen = selected_plan_id if selected_plan_id is not None else proposal.selected_plan_id
    return validator.validate(
        chosen,
        # The set is the *eligible* one, not every candidate: a plan a hard
        # constraint excluded is not back in play because something picked it.
        candidate_plan_ids=[e.plan_id for e in allowed],
        plans=context["plans"],
        graphs=context["graphs"],
        evaluations=context["evaluation_map"],
        definitions=ctx.services.get_all_definitions(),
        preference=context["preference"],
    )


def _rejection_reasons(rejected) -> list[str]:
    return [
        f"excluded {check.plan_id}: {'; '.join(check.violations)}" for check in rejected
    ]


def _approval_required(evaluation, preference) -> bool:
    """Whether policy or plan metadata requires a person to choose."""
    return bool(
        getattr(preference, "require_human_approval", False)
        or getattr(evaluation, "human_approval_required", False)
    )


def _suspend_for_review(ctx, requirement, proposal) -> ProcessResult:
    """Persist an ordinary plan-review Continuation for any selection source."""
    return ctx.suspend(
        resume_point="await_review",
        waiting_for={
            "event_type": PLAN_SELECTION_REVIEWED,
            "proposal_id": str(proposal.id),
        },
        saved_process_state={
            "proposal_id": str(proposal.id),
            "work_requirement_id": str(requirement.id),
        },
        emitted_events=[
            ctx.new_event(
                "plan_selection_review_required",
                {
                    "proposal_id": str(proposal.id),
                    "work_requirement_id": str(requirement.id),
                    "selected_plan_id": str(proposal.selected_plan_id),
                    "candidate_plan_ids": [str(i) for i in proposal.candidate_plan_ids],
                },
            )
        ],
    )


# --- replanning -------------------------------------------------------------


async def replan_work(ctx: ProcessContext) -> ProcessResult:
    """Look for another way after a plan failed terminally.

    The premise (Invariant 79): *a plan failing is not the need failing*.  If
    the need still stands, the world is looked at **again** — current registry,
    current definitions, current state (Invariant 81) — and a fresh set of
    candidates is composed, excluding the shapes that have already failed.

    Nothing is rewritten.  The failed plan keeps its nodes, its statuses and
    its results; a replan produces a *new* plan (Invariant 80).  Whatever the
    failed plan already did to the world stays done (spec §98).
    """
    assert ctx.event is not None and ctx.services is not None
    payload = ctx.event.payload
    requirement_id = _uuid(payload.get("work_requirement_id"))
    previous_plan_id = _uuid(payload.get("plan_id"))
    requirement = ctx.services.get_work_requirement(requirement_id)
    if requirement is None:
        return ctx.fail(f"work requirement {requirement_id} not found")
    if requirement.resolved or requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW}:
        # A late or redelivered failure for work that has since been met
        # (spec §91).
        return ctx.complete(
            output={"replanned": False, "reason": f"work is {requirement.status.value}"}
        )

    decisions = ctx.services.get_decision_store()
    if decisions is None:
        return ctx.fail("the decision layer is not available")

    # Idempotency: the logical identity of an attempt is this need after that
    # plan (spec §93).  A redelivered replan_required finds it already made.
    if decisions.replan_attempt_exists(requirement.id, previous_plan_id):
        return ctx.complete(
            output={"replanned": False, "reason": "this replan was already attempted"}
        )

    attempt_number = requirement.replan_count + 1
    limit = (
        requirement.max_replans
        if requirement.max_replans is not None
        else ctx.services.get_default_max_replans()
    )
    if attempt_number > limit:
        return _replan_exhausted(ctx, requirement, previous_plan_id, attempt_number, limit)

    excluded = ctx.services.get_failed_plan_fingerprints(requirement.id)
    attempt = ReplanAttempt(
        work_requirement_id=requirement.id,
        previous_plan_id=previous_plan_id,
        attempt_number=attempt_number,
        excluded_fingerprints=list(excluded),
        failure_reason=_failure_reason(payload),
        reasons=[f"replanning after {previous_plan_id} failed"],
    )
    ctx.record_replan_attempt(attempt)
    ctx.count_replan(requirement.id, attempt_number)

    ctx.logger.info(
        "replanning %s (attempt %d/%d), excluding %d failed shape(s)",
        requirement.work_key,
        attempt_number,
        limit,
        len(excluded),
    )
    # Composition runs again from scratch against the *current* world, which is
    # how a capability registered since the failure gets used (spec §95) and
    # why no event replay is needed to find it (Invariant 81).
    return ctx.complete(
        output={
            "replanned": True,
            "attempt": attempt_number,
            "excluded_fingerprints": list(excluded),
        },
        emitted_events=[
            ctx.new_event(
                "composition_required",
                {
                    "work_requirement_id": str(requirement.id),
                    "exclude_fingerprints": list(excluded),
                    "replan_attempt": attempt_number,
                    "supersedes_plan_id": str(previous_plan_id)
                    if previous_plan_id
                    else None,
                },
            )
        ],
    )


def _replan_exhausted(ctx, requirement, previous_plan_id, attempt_number, limit):
    """Stop trying, and say so — without cancelling the need (Invariant 79)."""
    reason = f"replan limit reached ({limit})"
    ctx.record_replan_attempt(
        ReplanAttempt(
            work_requirement_id=requirement.id,
            previous_plan_id=previous_plan_id,
            attempt_number=attempt_number,
            failure_reason=reason,
            reasons=[reason],
        )
    )
    ctx.mark_work(requirement.id, WorkStatus.BLOCKED_PLAN)
    ctx.logger.info("replanning exhausted for %s: %s", requirement.work_key, reason)
    return ctx.complete(
        output={"replanned": False, "reason": reason},
        emitted_events=[
            ctx.new_event(
                REPLAN_UNAVAILABLE,
                {
                    "work_requirement_id": str(requirement.id),
                    "attempts": attempt_number - 1,
                    "reason": reason,
                },
            )
        ],
    )


def _failure_reason(payload) -> str | None:
    failed = payload.get("failed_nodes")
    if failed:
        return f"nodes failed: {failed}"
    return payload.get("reason")


# --- wiring -----------------------------------------------------------------


def bootstrap_decision(runtime) -> None:
    """Register the plan selector and the replanner."""
    runtime.register_process(SELECT_PROCESS_PLAN, select_process_plan)
    runtime.register_process(REPLAN_WORK, replan_work)


__all__ = [
    "BACKEND_NAME",
    "NO_VALID_PLAN",
    "PLAN_SELECTED",
    "PLAN_SELECTION_REVIEWED",
    "REPLAN_UNAVAILABLE",
    "REPLAN_WORK",
    "SELECT_PROCESS_PLAN",
    "bootstrap_decision",
    "replan_work",
    "select_process_plan",
]
