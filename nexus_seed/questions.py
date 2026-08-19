"""Answering a question NEXUS SEED asked about itself.

Phase 6 records what it does not know in ``self.unresolved_questions``.  A
person answering one is, like a review, nothing more than an event: the
``self_question_answered`` Event carries the answer back and the reflection
path picks it up from there.

Kept beside :mod:`nexus_seed.reviews` and for the same reason — the human
answer must not be reachable only through a command vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core.event import Event
from .presence.models import self_question_id


class QuestionAnswerError(ValueError):
    """Raised when the question is unknown, ambiguous, or the answer is empty."""


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    """One thing NEXUS SEED has said it does not know."""

    question_id: str
    question: Any

    @property
    def text(self) -> str:
        if isinstance(self.question, dict):
            return str(self.question.get("text") or self.question.get("question") or "")
        return str(self.question)


def open_questions(runtime) -> list[OpenQuestion]:
    """Return every unresolved self question, in the order it was recorded."""

    entry = runtime.state_store.get_current("self", "unresolved_questions")
    raw = entry.value if entry is not None else []
    if isinstance(raw, (list, tuple)):
        questions = list(raw)
    else:
        questions = [raw] if raw else []
    return [OpenQuestion(self_question_id(item), item) for item in questions]


def find_question(runtime, question_id) -> OpenQuestion | None:
    """Return the single open question ``question_id`` names, or ``None``."""

    wanted = str(question_id)
    matches = [item for item in open_questions(runtime) if item.question_id == wanted]
    if len(matches) > 1:
        raise QuestionAnswerError(f"self question {wanted!r} is ambiguous")
    return matches[0] if matches else None


async def answer_question(
    runtime, question_id, answer: str, *, actor: str = "human"
) -> Event | None:
    """Answer one open question, or return ``None`` when it is not open.

    Answering an already-answered question changes nothing, which is what a
    resubmitted form must do.
    """

    text = (answer or "").strip()
    if not text:
        raise QuestionAnswerError("an answer is required")
    question = find_question(runtime, question_id)
    if question is None:
        return None
    event = Event(
        type="self_question_answered",
        source=f"human:{actor}",
        payload={
            "question_id": question.question_id,
            "question": question.question,
            "answer": text,
        },
    )
    await runtime.submit_event(event)
    return event


__all__ = [
    "OpenQuestion",
    "QuestionAnswerError",
    "answer_question",
    "find_question",
    "open_questions",
]
