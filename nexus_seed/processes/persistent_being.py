"""Phase 6 as ordinary Processes composed on the Phase 5G runtime.

There is no agent loop here.  Durable Events wake finite Process activations;
when no Event, timer, retry or Continuation is due, the existing Runtime is
idle.  All state changes use Observation -> StateDelta -> ``apply_state_delta``
and all outward effects continue through the existing Action boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from ..core.event import Event, utcnow
from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..presence.models import (
    AttentionDisposition,
    ClaimStatus,
    ExperienceRecord,
    IntentionRecord,
    IntentionStatus,
    intention_id_for_pursuit,
    self_question_id,
)
from ..presence.projections import MASTER_CATEGORIES
from ..work.work_requirement import WorkStatus


DEFAULT_ATTENTION_EVENT_TYPES = (
    "existence_wakeup",
    "external_event",
    "external_signal",
    "human_message",
    "state_changed",
    "reflection_completed",
    "reconsideration_condition_met",
)

SELF_MASTER_STATE = ProcessDefinition(
    name="maintain_self_master_state",
    version="1",
    handler="maintain_self_master_state",
    trigger_event_types=(
        "self_state_observed",
        "master_claim_observed",
        "master_claim_inferred",
        "master_claim_confirmed",
        "self_question_answered",
    ),
    metadata={"role": "self_master_state_projection"},
)

ATTENTION_EVALUATION = ProcessDefinition(
    name="attention_evaluation",
    version="1",
    handler="attention_evaluation",
    trigger_event_types=DEFAULT_ATTENTION_EVENT_TYPES,
    metadata={"role": "attention"},
)

MAINTAIN_INTENTION = ProcessDefinition(
    name="maintain_intention",
    version="1",
    handler="maintain_intention",
    trigger_event_types=(
        "intention_declaration_requested",
        "intention_reconsideration_requested",
        "goal_created",
        "goal_resumed",
        "goal_paused",
        "goal_cancelled",
        "goal_blocked",
        "goal_achieved",
    ),
    metadata={"role": "intention_maintenance"},
)

RECORD_EXPERIENCE = ProcessDefinition(
    name="record_experience",
    version="1",
    handler="record_experience",
    trigger_event_types=(
        "action_succeeded",
        "action_failed",
        "action_rejected",
        "work_satisfied",
        "work_failed",
        "goal_achieved",
        "goal_blocked",
    ),
    metadata={"role": "experience_recorder"},
)

REFLECT_EXPERIENCE = ProcessDefinition(
    name="reflect_experience",
    version="1",
    handler="reflect_experience",
    trigger_event_types=("experience_recorded",),
    metadata={"role": "reflection"},
)


async def maintain_self_master_state(ctx: ProcessContext) -> ProcessResult:
    """Validate a Self/Master observation and propose a normal World State delta."""

    assert ctx.event is not None and ctx.services is not None
    payload = ctx.event.payload
    if ctx.event.type == "self_question_answered":
        question_id = str(payload.get("question_id") or "")
        answer = str(payload.get("answer") or "").strip()
        current_questions = ctx.services.get_current_state("self", "unresolved_questions")
        raw_questions = current_questions.value if current_questions is not None else []
        questions = (
            list(raw_questions)
            if isinstance(raw_questions, (list, tuple))
            else ([raw_questions] if raw_questions else [])
        )
        matched = [item for item in questions if self_question_id(item) == question_id]
        if len(matched) != 1 or not answer:
            return ctx.fail("Self question answer requires one current question and an answer")
        remaining = [item for item in questions if self_question_id(item) != question_id]
        emitted: list[Event] = []
        question_event = _propose_fact(
            ctx,
            entity="self",
            attribute="unresolved_questions",
            value=remaining,
            confidence=1.0,
            predicate="self_question_resolved",
            reason="authorized Master answer received through Control Plane",
        )
        if question_event is not None:
            emitted.append(question_event)
        current_beliefs = ctx.services.get_current_state("self", "beliefs")
        raw_beliefs = current_beliefs.value if current_beliefs is not None else []
        beliefs = (
            list(raw_beliefs)
            if isinstance(raw_beliefs, (list, tuple))
            else ([raw_beliefs] if raw_beliefs else [])
        )
        beliefs.append(
            {
                "question_id": question_id,
                "question": matched[0],
                "answer": answer,
                "claim_status": ClaimStatus.CONFIRMED.value,
                "source_event_id": str(ctx.event.id),
            }
        )
        belief_event = _propose_fact(
            ctx,
            entity="self",
            attribute="beliefs",
            value=beliefs,
            confidence=1.0,
            predicate="self_belief_confirmed",
            reason="authorized Master answer received through Control Plane",
        )
        if belief_event is not None:
            emitted.append(belief_event)
        emitted.append(
            ctx.new_event(
                "self_question_resolved",
                {"question_id": question_id, "source_event_id": str(ctx.event.id)},
            )
        )
        return ctx.complete(
            output={"question_id": question_id, "resolved": True},
            emitted_events=emitted,
        )
    if ctx.event.type == "self_state_observed":
        attribute = str(payload.get("attribute") or "").strip()
        allowed = {
            "identity",
            "current_concerns",
            "commitments",
            "unresolved_questions",
            "beliefs",
        }
        if attribute not in allowed:
            return ctx.fail(f"unsupported Self attribute {attribute!r}")
        event = _propose_fact(
            ctx,
            entity="self",
            attribute=attribute,
            value=payload.get("value"),
            confidence=_confidence(payload),
            predicate="self_state_observed",
            reason=str(payload.get("reason") or "Self observation"),
        )
        return ctx.complete(
            output={"entity": "self", "attribute": attribute, "changed": event is not None},
            emitted_events=[event] if event else [],
        )

    master_id = str(payload.get("master_id") or "").strip()
    category = str(payload.get("category") or "").strip()
    key = str(payload.get("key") or "").strip()
    if not master_id or category not in MASTER_CATEGORIES or not key:
        return ctx.fail("Master claim requires master_id, supported category, and key")
    suffix = ctx.event.type.removeprefix("master_claim_").upper()
    try:
        status = ClaimStatus(str(payload.get("claim_status") or suffix).upper())
    except ValueError:
        return ctx.fail("Master claim must be OBSERVED, INFERRED, or CONFIRMED")
    confidence = _confidence(payload)
    claim = {
        "value": payload.get("value"),
        "claim_status": status.value,
        "confidence": confidence,
        "source_event_id": str(ctx.event.id),
        "reason": str(payload.get("reason") or ""),
    }
    event = _propose_fact(
        ctx,
        entity=f"master:{master_id}",
        attribute=f"claim:{category}:{key}",
        value=claim,
        confidence=confidence,
        predicate="master_claim_recorded",
        reason=f"{status.value} Master claim",
    )
    return ctx.complete(
        output={"master_id": master_id, "claim_status": status.value, "changed": event is not None},
        emitted_events=[event] if event else [],
    )


async def attention_evaluation(ctx: ProcessContext) -> ProcessResult:
    """Classify one Event without requiring or generating Work."""

    assert ctx.event is not None and ctx.services is not None
    pursuits = ctx.services.get_active_pursuits()
    intentions = _intentions(ctx)
    disposition, reason, goal_ids = _attention_decision(ctx.event, pursuits, intentions, ctx)
    payload = {
        "source_event_id": str(ctx.event.id),
        "source_event_type": ctx.event.type,
        "disposition": disposition.value,
        "reason": reason,
        "goal_ids": [str(value) for value in goal_ids],
        "control_plane_priority_preserved": True,
    }
    emitted = [ctx.new_event("attention_evaluated", payload)]
    if disposition in {AttentionDisposition.RELEVANT, AttentionDisposition.RECONSIDER}:
        emitted.append(ctx.new_event("intention_reconsideration_requested", payload))
    elif disposition is AttentionDisposition.INVESTIGATE:
        emitted.append(ctx.new_event("attention_investigation_requested", payload))

    # Keep only the current meaningful focus in World State.  IGNORE is a
    # successful no-op, proving that attention need not manufacture Work.
    if disposition is not AttentionDisposition.IGNORE:
        state_event = _propose_fact(
            ctx,
            entity="self",
            attribute="attention",
            value=payload,
            confidence=1.0,
            predicate="attention_evaluated",
            reason=reason,
        )
        if state_event is not None:
            emitted.insert(0, state_event)
    return ctx.complete(output=payload, emitted_events=emitted)


async def maintain_intention(ctx: ProcessContext) -> ProcessResult:
    """Create or revise long-lived Intention State beneath what is pursued."""

    assert ctx.event is not None and ctx.services is not None
    event_type = ctx.event.type
    pursuit_ids = _pursuit_ids_for_intention_event(ctx)
    emitted: list[Event] = []
    changed: list[str] = []
    for pursuit_id in pursuit_ids:
        pursuit = ctx.services.get_pursuit(pursuit_id)
        if pursuit is None:
            continue
        current = _intention_for_pursuit(ctx, pursuit_id)
        next_record, should_evaluate = _next_intention(ctx, pursuit, current)
        if next_record is None:
            continue
        state_event = _propose_fact(
            ctx,
            entity=f"intention:{next_record.id}",
            attribute="record",
            value=next_record.to_dict(),
            confidence=1.0,
            predicate="intention_revised",
            reason=f"{event_type}: {next_record.reason}",
        )
        if state_event is not None:
            emitted.append(state_event)
            changed.append(str(next_record.id))
            emitted.append(
                ctx.new_event(
                    "intention_state_changed",
                    {
                        "intention_id": str(next_record.id),
                        "goal_id": str(pursuit_id),
                        "pursuit_id": str(pursuit_id),
                        "status": next_record.status.value,
                        "source_event_id": str(ctx.event.id),
                    },
                )
            )
        if should_evaluate and pursuit.active:
            emitted.append(
                ctx.new_event(
                    "goal_evaluation_requested",
                    {
                        "goal_id": str(pursuit_id),
                        "intention_id": str(next_record.id),
                        "source_event_id": str(ctx.event.id),
                    },
                )
            )
    if event_type == "intention_declaration_requested" and not pursuit_ids:
        return ctx.fail("intention declaration requires an existing goal_id")
    return ctx.complete(output={"intentions_changed": changed}, emitted_events=emitted)


async def record_experience(ctx: ProcessContext) -> ProcessResult:
    """Link existing traces in an Event; do not create a parallel Experience store."""

    assert ctx.event is not None and ctx.services is not None
    payload = ctx.event.payload
    proposal = None
    work = None
    proposal_id = _uuid(payload.get("action_proposal_id"))
    work_id = _uuid(payload.get("work_requirement_id"))
    if proposal_id is not None:
        proposal = ctx.services.get_action_proposal(proposal_id)
        if proposal is not None and proposal.source_work_requirement_id is not None:
            work_id = proposal.source_work_requirement_id
    if work_id is not None:
        work = ctx.services.get_work_requirement(work_id)
    goal_id = work.goal_id if work is not None else _uuid(payload.get("goal_id"))
    intention_id = intention_id_for_pursuit(goal_id) if goal_id else None
    action = {
        "action_proposal_id": str(proposal.id) if proposal else None,
        "action_execution_id": payload.get("action_execution_id"),
        "backend": proposal.backend if proposal else payload.get("backend"),
        "action_type": proposal.action_type if proposal else payload.get("action_type"),
        "target": proposal.target if proposal else None,
    }
    record = ExperienceRecord(
        source_event_id=ctx.event.id,
        situation={
            "event_type": ctx.event.type,
            "trigger_event_id": str(proposal.trigger_event_id) if proposal and proposal.trigger_event_id else None,
        },
        belief_before_action={
            "context_snapshot_id": (
                str(proposal.context_snapshot_id)
                if proposal and proposal.context_snapshot_id
                else None
            )
        },
        intention={
            "intention_id": str(intention_id) if intention_id else None,
            "goal_id": str(goal_id) if goal_id else None,
            "work_requirement_id": str(work_id) if work_id else None,
        },
        action=action,
        reason=(proposal.rationale if proposal and proposal.rationale else str(payload.get("reason") or ctx.event.type)),
        result={
            "event_type": ctx.event.type,
            "result": payload.get("result"),
            "error": payload.get("error"),
            "work_status": work.status.value if work else None,
        },
        surprise=(
            payload.get("surprise")
            if "surprise" in payload
            else ctx.event.type in {"action_failed", "action_rejected", "work_failed", "goal_blocked"}
        ),
    )
    return ctx.complete(
        output={"source_event_id": str(ctx.event.id)},
        emitted_events=[ctx.new_event("experience_recorded", record.to_dict())],
    )


async def reflect_experience(ctx: ProcessContext) -> ProcessResult:
    """Derive a lesson through Observation/StateDelta, never a direct State write."""

    assert ctx.event is not None and ctx.services is not None
    try:
        experience = ExperienceRecord.from_dict(ctx.event.payload)
    except (KeyError, TypeError, ValueError) as exc:
        return ctx.fail(f"invalid experience record: {exc}")
    failed = bool(experience.result.get("error")) or bool(experience.surprise)
    if failed:
        lesson = "Reconsider the intention and assumptions before another attempt."
    else:
        lesson = "The validated intention-to-action path produced the expected result."
    reflection = {
        "experience_event_id": str(ctx.event.id),
        "source_event_id": str(experience.source_event_id),
        "lesson": lesson,
        "surprise": experience.surprise,
        "reconsider": failed,
        "reflected_at": utcnow().isoformat(),
    }
    state_event = _propose_fact(
        ctx,
        entity=f"experience:{ctx.event.id}",
        attribute="reflection",
        value=reflection,
        confidence=1.0,
        predicate="experience_reflected",
        reason="Reflection over existing experience trace",
    )
    emitted = [state_event] if state_event else []
    emitted.append(ctx.new_event("reflection_completed", reflection))
    return ctx.complete(output=reflection, emitted_events=emitted)


def bootstrap_persistent_being(
    runtime,
    *,
    enabled: bool,
    wake_on_start: bool = True,
    attention_event_types: tuple[str, ...] | None = None,
) -> bool:
    """Register Phase 6 Processes only when its feature flag is enabled.

    Returns whether Phase 6 was enabled.  Calling this with ``enabled=False``
    is a strict no-op: no definitions, state, events, or Runtime settings are
    changed, preserving Phase 5G behavior exactly.
    """

    if not enabled:
        return False
    if getattr(runtime, "_phase6_bootstrapped", False):
        return True
    attention = ATTENTION_EVALUATION
    if attention_event_types is not None:
        attention = replace(attention, trigger_event_types=tuple(attention_event_types))
    runtime.register_process(SELF_MASTER_STATE, maintain_self_master_state)
    runtime.register_process(attention, attention_evaluation)
    runtime.register_process(MAINTAIN_INTENTION, maintain_intention)
    runtime.register_process(RECORD_EXPERIENCE, record_experience)
    runtime.register_process(REFLECT_EXPERIENCE, reflect_experience)
    runtime._phase6_bootstrapped = True
    if wake_on_start:
        active_goals = runtime.active_pursuits()
        unresolved_intentions = [
            entry
            for entry in runtime.state_store.all_current()
            if entry.entity.startswith("intention:")
            and entry.attribute == "record"
            and isinstance(entry.value, dict)
            and entry.value.get("status") not in {"SATISFIED", "ABANDONED"}
        ]
        questions = runtime.state_store.get("self", "unresolved_questions", [])
        if active_goals or unresolved_intentions or questions:
            runtime.event_store.append(
                Event(
                    type="existence_wakeup",
                    source="process.persistent_being",
                    payload={
                        "reason": "startup_recovery",
                        "active_goal_count": len(active_goals),
                        "unresolved_intention_count": len(unresolved_intentions),
                        "unresolved_question_count": len(questions or []),
                    },
                )
            )
    return True


def _propose_fact(
    ctx: ProcessContext,
    *,
    entity: str,
    attribute: str,
    value: Any,
    confidence: float,
    predicate: str,
    reason: str,
) -> Event | None:
    current = ctx.services.get_current_state(entity, attribute)
    old_value = current.value if current is not None else None
    if current is not None and old_value == value:
        return None
    observation = ctx.observe(
        subject=entity,
        predicate=predicate,
        extracted={"attribute": attribute, "value": value, "reason": reason},
        confidence=confidence,
    )
    delta = ctx.propose_delta(
        entity=entity,
        attribute=attribute,
        old_value=old_value,
        new_value=value,
        observation=observation,
        confidence=confidence,
        reason=reason,
    )
    return ctx.new_event(
        "state_delta_created",
        {
            "entity": entity,
            "attribute": attribute,
            "old_value": old_value,
            "new_value": value,
            "source_event_id": str(delta.source_event_id) if delta.source_event_id else None,
            "observation_id": str(delta.observation_id) if delta.observation_id else None,
            "state_delta_id": str(delta.id),
            "confidence": confidence,
        },
    )


def _attention_decision(event, pursuits, intentions, ctx):
    goal_ids = [item.id for item in pursuits]
    goals = pursuits
    if event.type == "state_changed":
        entity = str(event.payload.get("entity") or "")
        attribute = event.payload.get("attribute")
        if (entity == "self" and attribute == "attention") or entity.startswith(("intention:", "experience:")):
            return AttentionDisposition.IGNORE, "Phase 6's own projection changed", []
    if event.type in {"goal_created", "goal_resumed", "goal_evaluation_requested"}:
        return AttentionDisposition.IGNORE, "Goal lifecycle is owned by Control Plane and intention maintenance", []
    if event.type == "reconsideration_condition_met":
        selected = _payload_goal_ids(event.payload) or goal_ids
        return AttentionDisposition.RECONSIDER, "persisted reconsideration condition became true", selected
    if event.type == "existence_wakeup":
        active = [value for value in intentions if value.status is IntentionStatus.ACTIVE]
        if active:
            return AttentionDisposition.RECONSIDER, "active intention survived restart", [value.pursuit_id for value in active]
        if goals and not intentions:
            return AttentionDisposition.RECONSIDER, "active Goal has no maintained intention", goal_ids
        questions = ctx.services.get_current_state("self", "unresolved_questions")
        if questions is not None and questions.value:
            return AttentionDisposition.INVESTIGATE, "Self has unresolved questions", goal_ids
        return AttentionDisposition.IGNORE, "waiting intentions have no startup condition", []
    if event.type == "reflection_completed":
        if event.payload.get("reconsider"):
            return AttentionDisposition.RECONSIDER, "reflection found a surprise", goal_ids
        return AttentionDisposition.IGNORE, "reflection confirmed expectations", []
    matching = [
        value.pursuit_id
        for value in intentions
        if value.status is IntentionStatus.WAITING and event.type in value.reconsider_on
    ]
    if matching:
        return AttentionDisposition.RELEVANT, "event matches a persisted reconsideration condition", matching
    explicit = _payload_goal_ids(event.payload)
    if explicit:
        return AttentionDisposition.RELEVANT, "event explicitly references a Goal", explicit
    if event.payload.get("relevant") is True or str(event.payload.get("importance", "")).lower() in {"high", "critical"}:
        return AttentionDisposition.RELEVANT, "event declares high relevance", goal_ids
    if event.type in {"external_event", "external_signal", "human_message"} and goals:
        return AttentionDisposition.INVESTIGATE, "external event may affect an active Goal", goal_ids
    if event.type in {"work_satisfied", "work_failed", "action_failed"}:
        return AttentionDisposition.IGNORE, "existing Goal and reflection Processes own this outcome", []
    if event.type == "state_changed" and goals:
        return AttentionDisposition.RECONSIDER, "World State changed while a Goal is active", goal_ids
    return AttentionDisposition.IGNORE, "no active concern or condition matched", []


def _next_intention(ctx, pursuit, current):
    event_type = ctx.event.type
    payload = ctx.event.payload
    should_evaluate = False
    if event_type == "intention_declaration_requested":
        try:
            status = IntentionStatus(str(payload.get("status", "ACTIVE")).upper())
        except ValueError:
            status = IntentionStatus.ACTIVE
        record = IntentionRecord.for_pursuit(
            pursuit.id,
            str(payload.get("focus") or pursuit.objective),
            status=status,
            reason=str(payload.get("reason") or "explicit intention declaration"),
            reconsider_on=tuple(str(value) for value in payload.get("reconsider_on", ()) or ()),
        )
        return record, status is IntentionStatus.ACTIVE
    if current is None:
        record = IntentionRecord.for_pursuit(
            pursuit.id,
            pursuit.objective,
            status=IntentionStatus.ACTIVE,
            reason="an active pursuit requires a current intention",
            reconsider_on=pursuit.reconsider_on,
        )
        return record, event_type in {"goal_created", "goal_resumed", "intention_reconsideration_requested"}
    status = current.status
    reason = current.reason
    work_ids = list(current.work_requirement_ids)
    attention_id = current.last_attention_event_id
    if event_type in {"goal_cancelled"}:
        status, reason = IntentionStatus.ABANDONED, "Goal cancelled by Control Plane"
    elif event_type == "goal_achieved":
        status, reason = IntentionStatus.SATISFIED, "Goal achieved"
    elif event_type in {"goal_paused"}:
        status, reason = IntentionStatus.WAITING, "Goal paused by Control Plane"
    elif event_type == "goal_blocked":
        status, reason = IntentionStatus.BLOCKED, "Goal is blocked"
    elif event_type == "goal_work_generated":
        work_id = _uuid(payload.get("work_requirement_id"))
        if work_id and work_id not in work_ids:
            work_ids.append(work_id)
        status, reason = IntentionStatus.ACTIVE, "Goal gap produced existing Work"
    elif event_type == "work_failed":
        status, reason = IntentionStatus.BLOCKED, "Work for the intention failed"
    elif event_type == "work_satisfied":
        status, reason = IntentionStatus.ACTIVE, "Work completed; Goal must be reevaluated"
    elif event_type in {"intention_reconsideration_requested", "goal_resumed"}:
        if current.status is IntentionStatus.ACTIVE:
            return current, True
        status = IntentionStatus.ACTIVE
        reason = str(payload.get("reason") or "attention requested reconsideration")
        attention_id = _uuid(payload.get("source_event_id"))
        should_evaluate = True
    record = replace(
        current,
        status=status,
        reason=reason,
        work_requirement_ids=tuple(work_ids),
        last_attention_event_id=attention_id,
        updated_at=utcnow(),
    )
    return record, should_evaluate


def _pursuit_ids_for_intention_event(ctx) -> list[str]:
    ids = _payload_goal_ids(ctx.event.payload)
    if ids:
        return ids
    work_id = _uuid(ctx.event.payload.get("work_requirement_id"))
    if work_id and ctx.services:
        work = ctx.services.get_work_requirement(work_id)
        if work is not None and work.goal_id is not None:
            return [str(work.goal_id)]
    if ctx.event.type == "intention_reconsideration_requested":
        return [item.id for item in ctx.services.get_active_pursuits()]
    return []


def _intentions(ctx) -> list[IntentionRecord]:
    records = []
    for entry in ctx.services.get_current_state_by_prefix("intention:"):
        if entry.attribute != "record" or not isinstance(entry.value, dict):
            continue
        try:
            records.append(IntentionRecord.from_dict(entry.value))
        except (KeyError, TypeError, ValueError):
            continue
    return records


def _intention_for_pursuit(ctx, pursuit_id):
    entry = ctx.services.get_current_state(
        f"intention:{intention_id_for_pursuit(pursuit_id)}", "record"
    )
    if entry is None or not isinstance(entry.value, dict):
        return None
    try:
        return IntentionRecord.from_dict(entry.value)
    except (KeyError, TypeError, ValueError):
        return None


def _payload_goal_ids(payload) -> list[str]:
    """Pursuit ids an event names, under either key.

    ``goal_id``/``goal_ids`` stay readable because events already in a journal
    carry them; a pursuit id is text now, so it is no longer parsed as a UUID.
    """

    values = (
        payload.get("pursuit_ids")
        or payload.get("goal_ids")
        or [payload.get("pursuit_id") or payload.get("goal_id")]
    )
    return [str(item) for item in values if item]


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _confidence(payload) -> float:
    try:
        value = float(payload.get("confidence", 1.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


__all__ = [
    "ATTENTION_EVALUATION",
    "DEFAULT_ATTENTION_EVENT_TYPES",
    "MAINTAIN_INTENTION",
    "RECORD_EXPERIENCE",
    "REFLECT_EXPERIENCE",
    "SELF_MASTER_STATE",
    "attention_evaluation",
    "bootstrap_persistent_being",
    "maintain_intention",
    "maintain_self_master_state",
    "record_experience",
    "reflect_experience",
]
