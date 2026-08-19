"""A person approves constrained Work before it starts.

This is not a Goal concern and never was: a WorkRequirement marked
``human_review_required`` waits on an ordinary Event + Continuation, and the
review is settled through :mod:`nexus_seed.reviews` like any other.
"""

from __future__ import annotations

import uuid

from ..core.process import ProcessContext, ProcessDefinition, ProcessResult
from ..work.work_requirement import WorkStatus


REVIEW_HUMAN_WORK = ProcessDefinition(
    name="review_human_work",
    version="1",
    handler="review_human_work",
    trigger_event_types=("work_review_required",),
    metadata={"role": "human_work_review"},
)


async def review_human_work(ctx: ProcessContext) -> ProcessResult:
    """Enforce a normal Event + Continuation before constrained Work starts."""

    assert ctx.event is not None and ctx.services is not None
    work_id = _uuid(
        ctx.saved_process_state.get("work_requirement_id")
        or ctx.event.payload.get("work_requirement_id")
    )
    work = ctx.services.get_work_requirement(work_id) if work_id else None
    if work is None:
        return ctx.fail(f"reviewed work {work_id} not found")
    if ctx.resume_point is None:
        if work.status is not WorkStatus.WAITING_REVIEW:
            return ctx.complete(output={"waiting": False, "status": work.status.value})
        return ctx.suspend(
            resume_point="await_work_review",
            waiting_for={"event_type": "work_reviewed", "work_requirement_id": str(work.id)},
            saved_process_state={"work_requirement_id": str(work.id)},
        )
    decision = str(ctx.event.payload.get("decision", "reject")).lower()
    if decision != "approve":
        ctx.mark_work(work.id, WorkStatus.CANCELLED)
        return ctx.complete(
            output={"approved": False},
            emitted_events=[ctx.new_event("work_cancelled", {
                "work_requirement_id": str(work.id), "reason": "human review rejected"
            })],
        )
    ctx.mark_work(work.id, WorkStatus.EXPECTED)
    return ctx.complete(
        output={"approved": True},
        emitted_events=[ctx.new_event("work_required", {"work_requirement_id": str(work.id)})],
    )


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def bootstrap_work_review(runtime) -> None:
    """Register the human review gate for constrained Work."""

    runtime.register_process(REVIEW_HUMAN_WORK, review_human_work)


__all__ = [
    "REVIEW_HUMAN_WORK",
    "bootstrap_work_review",
    "review_human_work",
]
