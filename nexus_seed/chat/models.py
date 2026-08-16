"""Project Chat records — a conversation about a project, never a fact about it.

A chat message is what a human asked and what NEXUS SEED answered.  It is kept
apart from Observations, StateDeltas and World State on purpose: an answer is a
*rendering* of the current Project Situation, so treating it as a confirmed
world fact would let a wording mistake become durable truth (Invariant 192).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
import uuid

from ..core.event import utcnow


class ChatRole(str, Enum):
    """Who produced one message."""

    HUMAN = "HUMAN"
    NEXUS_SEED = "NEXUS_SEED"


class ChatAnswerStatus(str, Enum):
    """How an answer was produced, so the interface never has to guess."""

    #: The configured LLM answered over the Project Situation projection.
    ANSWERED = "ANSWERED"
    #: The request would change project state; this phase is read-only.
    READ_ONLY_REFUSED = "READ_ONLY_REFUSED"
    #: The question referred to a different project than this thread.
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    #: No LLM backend is configured; only deterministic facts were returned.
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    #: The LLM was reached but could not complete the call.
    LLM_FAILED = "LLM_FAILED"
    #: The LLM answered with output that did not match the answer schema.
    LLM_INVALID = "LLM_INVALID"

    @property
    def answered_by_llm(self) -> bool:
        """Whether the text came from the LLM rather than from projection facts."""

        return self is ChatAnswerStatus.ANSWERED


class ChatCertainty(str, Enum):
    """How the answer relates to the projection it was compiled from."""

    #: Stated directly by the Project Situation projection.
    FACT = "FACT"
    #: Reasoned from projection facts and marked as reasoning.
    INFERENCE = "INFERENCE"
    #: The projection does not answer the question.
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class ProjectChatMessage:
    """One durable turn of a project-scoped conversation."""

    thread_id: uuid.UUID
    project_id: str
    role: ChatRole
    text: str
    status: ChatAnswerStatus = ChatAnswerStatus.ANSWERED
    certainty: ChatCertainty | None = None
    references: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation used by HTTP and the Cockpit."""

        return {
            "id": str(self.id),
            "thread_id": str(self.thread_id),
            "project_id": self.project_id,
            "role": self.role.value,
            "text": self.text,
            "status": self.status.value,
            "certainty": self.certainty.value if self.certainty else None,
            "references": list(self.references),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class ProjectChatThread:
    """The single conversation belonging to one project."""

    project_id: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of the thread itself."""

        return {
            "thread_id": str(self.id),
            "project_id": self.project_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


__all__ = [
    "ChatAnswerStatus",
    "ChatCertainty",
    "ChatRole",
    "ProjectChatMessage",
    "ProjectChatThread",
]
