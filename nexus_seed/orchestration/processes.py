"""The coordination Process: say so when the loop cannot turn on its own.

Every other step of the loop already has an owner, and this module deliberately
re-implements none of them.  What was missing is the last step of the cycle:
when automatic Capability Acquisition stops, the durable records explain it but
nothing *states* it, so a person has to go looking.

``request_human_intervention`` turns that stop into one ordinary Event naming
the Goal being pursued, the Task that stopped, the Capability that is missing
and what was already tried.  It reads existing records and emits an Event; it
changes no Work, Goal, Capability or World State (Invariant 207).
"""

from __future__ import annotations

import uuid

from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..processes.autonomy import ACQUISITION_BLOCKED

#: Emitted when NEXUS SEED cannot continue a Goal without a person.
HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"

REQUEST_HUMAN_INTERVENTION = ProcessDefinition(
    name="request_human_intervention",
    version="1",
    handler="request_human_intervention",
    trigger_event_types=(ACQUISITION_BLOCKED, "provider_missing"),
    metadata={"role": "goal_loop_coordinator"},
)


async def request_human_intervention(ctx: ProcessContext) -> ProcessResult:
    """Announce that a Goal needs a person, using the records that say why."""

    assert ctx.event is not None and ctx.services is not None
    payload = ctx.event.payload
    work_id = _uuid(payload.get("work_requirement_id"))
    work = ctx.services.get_work_requirement(work_id) if work_id else None
    if work is None:
        return ctx.complete(output={"requested": False, "reason": "work not found"})

    goal = ctx.services.get_goal(work.goal_id) if work.goal_id else None
    session = _session(ctx, payload)
    gaps = ctx.services.get_capability_gaps_for_work(work.id)
    missing = sorted(
        {
            *work.missing_capabilities,
            *(name for gap in gaps for name in gap.missing_names),
            *(str(value) for value in payload.get("missing_capabilities", ()) or ()),
        }
    )
    request = {
        "goal_id": str(goal.id) if goal is not None else None,
        "goal_title": goal.title if goal is not None else None,
        "goal_objective": goal.objective if goal is not None else None,
        "project_id": work.project or (goal.metadata.get("project_id") if goal else None),
        "work_requirement_id": str(work.id),
        "work_objective": work.objective or work.work_type,
        "work_status": work.status.value,
        "missing_capabilities": missing,
        "reason": _reason(payload, session),
        "tried": _tried(ctx, session),
        "acquisition_session_id": str(session.id) if session is not None else None,
        "next_action": (
            "Provide or enable a Skill / Process / Provider for "
            + (", ".join(missing) or "the missing capability")
            + ", or narrow the Goal."
        ),
    }
    return ctx.complete(
        output={"requested": True, "work_requirement_id": str(work.id)},
        emitted_events=[ctx.new_event(HUMAN_INTERVENTION_REQUIRED, request)],
    )


def _session(ctx: ProcessContext, payload: dict):
    """Read the acquisition session this stop belongs to, if there is one."""

    session_id = _uuid(payload.get("acquisition_session_id"))
    if session_id is None or ctx.services is None:
        return None
    return ctx.services.get_acquisition_session(session_id)


def _reason(payload: dict, session) -> str:
    """Say why the loop stopped, preferring the record that decided it."""

    if session is not None and session.blocked_reason:
        return str(session.blocked_reason)
    if payload.get("blocked_reason"):
        return str(payload["blocked_reason"])
    reasons = payload.get("reasons")
    if isinstance(reasons, (list, tuple)) and reasons:
        return "; ".join(str(value) for value in reasons)
    return "automatic capability acquisition cannot continue"


def _tried(ctx: ProcessContext, session) -> list[str]:
    """Summarise what automation already attempted, from its own trace."""

    if session is None or ctx.services is None:
        return []
    trace = ctx.services.get_acquisition_trace(session.id)
    if trace is None:
        return []
    lines = [
        f"acquisition reached {trace.session.current_stage.value} "
        f"({trace.session.status.value})"
    ]
    if trace.extension_proposal is not None:
        lines.append(
            f"considered strategy {trace.extension_proposal.declared_strategy} "
            f"({trace.extension_proposal.status.value})"
        )
    lines.extend(
        f"{decision.stage.value} policy={decision.decision.value}"
        + (f": {', '.join(decision.reasons)}" if decision.reasons else "")
        for decision in trace.decisions
    )
    lines.extend(
        f"{attempt.attempt_type} attempt {attempt.attempt_number} {attempt.status}"
        + (f": {attempt.failure_reason}" if attempt.failure_reason else "")
        for attempt in trace.attempts
    )
    return lines


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, TypeError):
        return None


def bootstrap_orchestration(runtime) -> None:
    """Register the Goal-loop coordination Process (idempotent)."""

    runtime.register_process(REQUEST_HUMAN_INTERVENTION, request_human_intervention)


__all__ = [
    "HUMAN_INTERVENTION_REQUIRED",
    "REQUEST_HUMAN_INTERVENTION",
    "bootstrap_orchestration",
    "request_human_intervention",
]
