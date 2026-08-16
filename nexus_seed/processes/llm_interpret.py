"""interpret_event_llm — interpret a natural-language event via an LLM backend.

This is an ordinary Process.  It calls a swappable :class:`ExecutionBackend`
(the LLM), turns the structured output into an :class:`InterpretationProposal`,
and runs it through validation + policy.  Only an ACCEPTED proposal becomes an
Observation + StateDelta, which flow through the *existing* Phase 2B
``apply_state_delta`` pipeline — the LLM has no private path to world state
(Invariants 16–19).

Decisions:

* **ACCEPT** — emit Observation + StateDelta, complete.
* **REVIEW** — suspend on a normal Continuation waiting for a human
  ``interpretation_reviewed`` event (Invariant 18).
* **REJECT** — record the proposal, change nothing, complete.

Backend failure or invalid structured output returns a retryable result so the
Phase 2A retry mechanism applies (no bespoke retry loop; spec §38–§39).  Every
call is recorded as an :class:`LLMInvocation` first — success *or* failure — so
a proposal that never existed still leaves a trace of having been attempted
(Phase 3C spec §69).
"""

from __future__ import annotations

import uuid

from ..backends.base import BackendRequest, LLMInvocation
from ..context.requirements import ContextRequirements, EventsReq
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..intelligence.policy import InterpretationPolicy
from ..intelligence.proposal import InterpretationProposal, ProposalDecision
from ..intelligence.validation import validate_proposal
from ..world.observation import Observation
from ..world.state_delta import StateDelta

#: The name a handler looks up in ``ctx.backends``.
BACKEND_NAME = "llm"

INSTRUCTION = (
    "Read the message and report what changed in the world as structured data. "
    "Return subject, predicate, confidence (0-1), rationale, and "
    "proposed_state_deltas (entity, attribute, old_value, new_value, unit, confidence). "
    "Always include proposed_state_deltas. If no state change is warranted, return "
    '"proposed_state_deltas": []. Never invent a state change to make the array non-empty.'
)

PROPOSAL_SCHEMA = {
    "type": "object",
    "required": ["subject", "confidence", "proposed_state_deltas"],
    "properties": {
        "subject": {"type": "string", "minLength": 1},
        "predicate": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": ["string", "null"]},
        "proposed_state_deltas": {
            "type": "array",
            "description": (
                "Required. Use an empty array when no state change is warranted; "
                "never invent a delta."
            ),
            "items": {
                "type": "object",
                "required": ["entity", "attribute", "confidence"],
                "properties": {
                    "entity": {"type": "string", "minLength": 1},
                    "attribute": {"type": "string", "minLength": 1},
                    "old_value": {},
                    "new_value": {},
                    "unit": {"type": ["string", "null"]},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
            },
        },
    },
}

DEFAULT_POLICY = InterpretationPolicy(accept_threshold=0.85, review_threshold=0.60)

INTERPRET_LLM = ProcessDefinition(
    name="interpret_event_llm",
    version="1",
    handler="interpret_event_llm",
    trigger_event_types=("human_message",),
    max_retries=2,
    metadata={"role": "interpreter", "backend": BACKEND_NAME},
    context_requirements=ContextRequirements(
        include_trigger_event=True, events=EventsReq(recent=5)
    ),
)


def _uuid(value) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _current_value(ctx: ProcessContext):
    def lookup(entity: str, attribute: str):
        if ctx.services is None:
            return None
        entry = ctx.services.get_current_state(entity, attribute)
        return entry.value if entry is not None else None

    return lookup


async def interpret_event_llm(ctx: ProcessContext) -> ProcessResult:
    """Interpret a raw event via LLM, or handle a human review on resume."""
    if ctx.resume_point == "await_review":
        return _handle_review(ctx)
    return await _handle_fresh(ctx)


async def _handle_fresh(ctx: ProcessContext) -> ProcessResult:
    backend = ctx.backends.get(BACKEND_NAME) if ctx.backends else None
    if backend is None:
        return ctx.fail(f"no {BACKEND_NAME!r} backend registered")

    request = BackendRequest(
        instruction=INSTRUCTION,
        context=ctx.view.to_snapshot_dict() if ctx.view else {},
        output_schema=PROPOSAL_SCHEMA,
        metadata={
            "trigger_event_type": ctx.event.type if ctx.event else None,
            "text": ctx.event.payload.get("text") if ctx.event else None,
        },
    )
    result = await backend.execute(request)

    # Record the attempt before judging it.  Invocations are an audit journal,
    # so they survive the rollback a retry causes (spec §69) — otherwise every
    # failed call would be invisible in the very log meant to explain failures.
    invocation = LLMInvocation(
        process_instance_id=ctx.instance.id,
        backend=BACKEND_NAME,
        activation_id=ctx.activation_id,
        model=result.model,
        request_metadata={"trigger_event_type": request.metadata.get("trigger_event_type")},
        response_metadata={"usage": result.usage, "latency_ms": result.latency_ms},
        context_snapshot_id=ctx.context_snapshot_id,
        success=result.success,
        error=result.error,
    )
    ctx.record_llm_invocation(invocation)

    # Backend failure -> retry (Phase 2A). No partial anything is persisted.
    if not result.success:
        return ctx.retry(f"backend failure: {result.error}")

    proposal = InterpretationProposal.from_output(
        result.parsed_output,
        source_event_id=ctx.event.id if ctx.event else None,
        created_by_process_id=ctx.instance.id,
    )
    if proposal is None:
        invocation.success = False
        invocation.error = "unparseable structured output"
        return ctx.retry("backend returned unparseable structured output")

    validation = validate_proposal(proposal, current_value=_current_value(ctx))
    if not validation.schema_ok:
        # Schema failure is retryable; nothing but the journal is committed.
        invocation.success = False
        invocation.error = "schema validation failed: " + "; ".join(validation.reasons)
        return ctx.retry(invocation.error)

    decision = DEFAULT_POLICY.decide(proposal.confidence, validation.consistency_ok)
    proposal.llm_invocation_id = invocation.id
    proposal.context_snapshot_id = ctx.context_snapshot_id
    proposal.decision = decision
    ctx.record_proposal(proposal)

    if decision is ProposalDecision.ACCEPT:
        emitted = _emit_accept(ctx, proposal)
        return ctx.complete(
            output={"decision": "ACCEPT", "proposal_id": str(proposal.id)},
            emitted_events=emitted,
        )
    if decision is ProposalDecision.REVIEW:
        ctx.logger.info("proposal %s -> REVIEW (confidence %.2f)", proposal.id, proposal.confidence)
        return ctx.suspend(
            resume_point="await_review",
            waiting_for={"event_type": "interpretation_reviewed", "proposal_id": str(proposal.id)},
            saved_process_state={"proposal_id": str(proposal.id)},
        )
    rejected = ctx.new_event("interpretation_rejected", {"proposal_id": str(proposal.id)})
    return ctx.complete(
        output={"decision": "REJECT", "proposal_id": str(proposal.id)},
        emitted_events=[rejected],
    )


def _handle_review(ctx: ProcessContext) -> ProcessResult:
    assert ctx.event is not None
    payload = ctx.event.payload
    proposal_id = _uuid(payload.get("proposal_id"))
    human_decision = payload.get("decision", "reject")

    proposal = ctx.services.get_proposal(proposal_id) if ctx.services else None
    if proposal is None:
        return ctx.fail(f"proposal {proposal_id} not found on review")

    if human_decision == "modify":
        replacement = payload.get("replacement")
        new_proposal = InterpretationProposal.from_output(
            replacement,
            source_event_id=proposal.source_event_id,
            created_by_process_id=ctx.instance.id,
        )
        if new_proposal is None:
            return ctx.fail("modify replacement is not a valid proposal")
        ctx.update_proposal(proposal_id, ProposalDecision.MODIFIED)
        validation = validate_proposal(new_proposal, current_value=_current_value(ctx))
        decision = (
            DEFAULT_POLICY.decide(new_proposal.confidence, validation.consistency_ok)
            if validation.schema_ok
            else ProposalDecision.REJECT
        )
        new_proposal.decision = decision
        ctx.record_proposal(new_proposal)
        if decision is ProposalDecision.ACCEPT:
            return ctx.complete(
                output={"decision": "ACCEPT", "proposal_id": str(new_proposal.id)},
                emitted_events=_emit_accept(ctx, new_proposal),
            )
        return ctx.complete(
            output={"decision": decision.value, "proposal_id": str(new_proposal.id)}
        )

    if human_decision == "approve":
        # Re-validate against CURRENT state before accepting (spec §26).
        validation = validate_proposal(proposal, current_value=_current_value(ctx))
        if not validation.schema_ok or not validation.consistency_ok:
            ctx.update_proposal(proposal_id, ProposalDecision.REJECT)
            return ctx.complete(
                output={"decision": "REJECT", "reason": "revalidation_failed"},
                emitted_events=[ctx.new_event("interpretation_rejected", {"proposal_id": str(proposal_id)})],
            )
        ctx.update_proposal(proposal_id, ProposalDecision.ACCEPT)
        return ctx.complete(
            output={"decision": "ACCEPT", "proposal_id": str(proposal_id)},
            emitted_events=_emit_accept(ctx, proposal),
        )

    # reject
    ctx.update_proposal(proposal_id, ProposalDecision.REJECT)
    return ctx.complete(
        output={"decision": "REJECT", "proposal_id": str(proposal_id)},
        emitted_events=[ctx.new_event("interpretation_rejected", {"proposal_id": str(proposal_id)})],
    )


def _emit_accept(ctx: ProcessContext, proposal: InterpretationProposal) -> list:
    """Stage the Observation + StateDeltas and emit state_delta_created events.

    Observation/StateDelta carry the *original* source event id (the raw
    message), so provenance points at the world event, not the review event.
    """
    observation = Observation(
        subject=proposal.subject,
        predicate=proposal.predicate,
        extracted=dict(proposal.extracted),
        source_event_id=proposal.source_event_id,
        created_by_process_id=ctx.instance.id,
        confidence=proposal.confidence,
        proposal_id=proposal.id,
    )
    ctx.add_observation(observation)

    emitted = []
    for pd in proposal.proposed_state_deltas:
        delta = StateDelta(
            entity=pd.entity,
            attribute=pd.attribute,
            old_value=pd.old_value,
            new_value=pd.new_value,
            source_event_id=proposal.source_event_id,
            observation_id=observation.id,
            created_by_process_id=ctx.instance.id,
            confidence=pd.confidence,
            reason=proposal.rationale,
        )
        ctx.add_state_delta(delta)
        emitted.append(
            ctx.new_event(
                "state_delta_created",
                {
                    "entity": pd.entity,
                    "attribute": pd.attribute,
                    "old_value": pd.old_value,
                    "new_value": pd.new_value,
                    "source_event_id": str(proposal.source_event_id) if proposal.source_event_id else None,
                    "observation_id": str(observation.id),
                    "state_delta_id": str(delta.id),
                    "confidence": pd.confidence,
                    "unit": pd.unit,
                },
            )
        )
    return emitted


def bootstrap_llm_interpreter(runtime, backend) -> None:
    """Register the LLM interpreter process and its backend on ``runtime``."""
    runtime.register_backend(BACKEND_NAME, backend)
    runtime.register_process(INTERPRET_LLM, interpret_event_llm)
