"""Human review decisions, with no command vocabulary behind them.

A review is not a special kind of object: it is an ordinary Continuation that
happens to be waiting for an event whose type ends in ``_reviewed``.  Deciding
one is therefore just emitting that event.  That is the whole mechanism, and
keeping it here — rather than inside a command service — is what lets a person
approve something from any channel: the WebUI, an adapter, a script.

Nothing in this module authorizes anything.  Whoever can reach the channel can
decide, exactly as before; the gate is the channel's own (the webhook bearer
token, the CLI's shell), not a permission table that never denied anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .core.event import Event


#: The only two answers a review takes.  A third would be a new mechanism, not
#: a new string, so this stays closed.
DECISIONS = ("approve", "reject")


class ReviewDecisionError(ValueError):
    """Raised when a review cannot be identified or the decision is not valid."""


@dataclass(frozen=True, slots=True)
class PendingReview:
    """One waiting decision, described by the event that would settle it."""

    review_id: str
    continuation_id: str
    process_instance_id: str
    event_type: str
    #: The condition's own fields minus ``event_type`` — what the resuming
    #: process matched on, and therefore what the emitted event must carry.
    condition: dict[str, Any] = field(default_factory=dict)

    def resume_event(self, decision: str, *, actor: str, note: str | None = None) -> Event:
        payload: dict[str, Any] = dict(self.condition)
        payload["decision"] = decision
        if note:
            payload["note"] = note
        return Event(type=self.event_type, source=f"human:{actor}", payload=payload)


def pending_reviews(runtime) -> list[PendingReview]:
    """Return every decision currently waiting on a person, oldest first."""

    found: list[tuple[Any, PendingReview]] = []
    for continuation in runtime.continuation_store.all():
        conditions = continuation.waiting_for.get("any") or [continuation.waiting_for]
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            event_type = str(condition.get("event_type") or "")
            if not event_type.endswith("_reviewed"):
                continue
            identifiers = [
                str(value) for key, value in condition.items() if key.endswith("_id")
            ]
            found.append((
                continuation.created_at,
                PendingReview(
                    review_id=identifiers[0] if identifiers else str(continuation.id),
                    continuation_id=str(continuation.id),
                    process_instance_id=str(continuation.process_instance_id),
                    event_type=event_type,
                    condition={
                        key: value for key, value in condition.items() if key != "event_type"
                    },
                ),
            ))
    return [review for _created_at, review in sorted(found, key=lambda item: item[0])]


def find_review(runtime, value) -> PendingReview | None:
    """Return the single review ``value`` names, or ``None``.

    An ambiguous reference is an error rather than a guess: approving the wrong
    thing is not recoverable by re-reading the screen.
    """

    wanted = str(value)
    matches = [
        review
        for review in pending_reviews(runtime)
        if wanted in {review.review_id, review.continuation_id}
    ]
    if len(matches) > 1:
        raise ReviewDecisionError(f"review target {wanted!r} is ambiguous")
    return matches[0] if matches else None


async def decide_review(
    runtime,
    review_id,
    decision: str,
    *,
    actor: str = "human",
    note: str | None = None,
) -> Event | None:
    """Settle one review and let the waiting process run on.

    Returns the emitted Event, or ``None`` when nothing is waiting on that
    identifier any more.  Deciding twice is therefore harmless: the second call
    finds no continuation and changes nothing, which is what a double-clicked
    approve button must do.
    """

    if decision not in DECISIONS:
        raise ReviewDecisionError(
            f"decision must be one of {', '.join(DECISIONS)}, not {decision!r}"
        )
    review = find_review(runtime, review_id)
    if review is None:
        return None
    event = review.resume_event(decision, actor=actor, note=note)
    await runtime.submit_event(event)
    return event


__all__ = [
    "DECISIONS",
    "PendingReview",
    "ReviewDecisionError",
    "decide_review",
    "find_review",
    "pending_reviews",
]
