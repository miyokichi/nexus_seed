"""Phase 6 semantic models; all are domain data, never Core primitives."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Any

from ..core.event import utcnow


INTENTION_NAMESPACE = uuid.UUID("7106a2b9-83be-4bf4-8f08-a96afdd27261")


class ClaimStatus(str, Enum):
    """Epistemic status required on every claim about the Master."""

    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    CONFIRMED = "CONFIRMED"


class AttentionDisposition(str, Enum):
    """Deterministic result of one attention evaluation."""

    RELEVANT = "RELEVANT"
    IGNORE = "IGNORE"
    INVESTIGATE = "INVESTIGATE"
    RECONSIDER = "RECONSIDER"


class IntentionStatus(str, Enum):
    """Lifecycle stored in an ``intention:<id>.record`` World State fact."""

    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    SATISFIED = "SATISFIED"
    BLOCKED = "BLOCKED"
    ABANDONED = "ABANDONED"

    @property
    def terminal(self) -> bool:
        """Whether this intention should no longer initiate activity."""

        return self in {IntentionStatus.SATISFIED, IntentionStatus.ABANDONED}


def intention_id_for_goal(goal_id: uuid.UUID | str) -> uuid.UUID:
    """Return the stable logical Intention id beneath one durable Goal."""

    return uuid.uuid5(INTENTION_NAMESPACE, str(goal_id))


def self_question_id(question: Any) -> str:
    """Return a stable UI/control identity for one unresolved Self question."""

    encoded = json.dumps(
        question, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class IntentionRecord:
    """Long-lived World State value describing the current pursuit of a Goal."""

    id: uuid.UUID
    goal_id: uuid.UUID
    focus: str
    status: IntentionStatus = IntentionStatus.ACTIVE
    reason: str = ""
    reconsider_on: tuple[str, ...] = ()
    work_requirement_ids: tuple[uuid.UUID, ...] = ()
    last_attention_event_id: uuid.UUID | None = None
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def for_goal(
        cls,
        goal_id: uuid.UUID,
        focus: str,
        *,
        status: IntentionStatus = IntentionStatus.ACTIVE,
        reason: str = "",
        reconsider_on: tuple[str, ...] = (),
    ) -> "IntentionRecord":
        """Build the one stable intention position associated with ``goal_id``."""

        return cls(
            id=intention_id_for_goal(goal_id),
            goal_id=goal_id,
            focus=focus,
            status=status,
            reason=reason,
            reconsider_on=reconsider_on,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize as the JSON-safe value stored in World State."""

        return {
            "id": str(self.id),
            "goal_id": str(self.goal_id),
            "focus": self.focus,
            "status": self.status.value,
            "reason": self.reason,
            "reconsider_on": list(self.reconsider_on),
            "work_requirement_ids": [str(value) for value in self.work_requirement_ids],
            "last_attention_event_id": (
                str(self.last_attention_event_id) if self.last_attention_event_id else None
            ),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntentionRecord":
        """Restore and validate an Intention from its World State value."""

        return cls(
            id=uuid.UUID(str(data["id"])),
            goal_id=uuid.UUID(str(data["goal_id"])),
            focus=str(data.get("focus") or ""),
            status=IntentionStatus(str(data.get("status", "ACTIVE")).upper()),
            reason=str(data.get("reason") or ""),
            reconsider_on=tuple(str(value) for value in data.get("reconsider_on", ()) or ()),
            work_requirement_ids=tuple(
                uuid.UUID(str(value)) for value in data.get("work_requirement_ids", ()) or ()
            ),
            last_attention_event_id=(
                uuid.UUID(str(data["last_attention_event_id"]))
                if data.get("last_attention_event_id")
                else None
            ),
            updated_at=(
                datetime.fromisoformat(str(data["updated_at"]))
                if data.get("updated_at")
                else utcnow()
            ),
        )


@dataclass(frozen=True, slots=True)
class MasterClaim:
    """One provenance-bearing claim in the Master World State projection."""

    category: str
    key: str
    value: Any
    status: ClaimStatus
    confidence: float
    source_event_id: uuid.UUID | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class MasterProjection:
    """Regenerable view of claims about one Master."""

    master_id: str
    goals: tuple[MasterClaim, ...] = ()
    preferences: tuple[MasterClaim, ...] = ()
    projects: tuple[MasterClaim, ...] = ()
    commitments: tuple[MasterClaim, ...] = ()
    concerns: tuple[MasterClaim, ...] = ()
    shared_history: tuple[MasterClaim, ...] = ()


@dataclass(frozen=True, slots=True)
class SelfProjection:
    """Regenerable view of Self; capabilities are projected, never copied."""

    identity: Any = None
    current_concerns: tuple[Any, ...] = ()
    commitments: tuple[Any, ...] = ()
    unresolved_questions: tuple[Any, ...] = ()
    beliefs: tuple[Any, ...] = ()
    active_goal_ids: tuple[uuid.UUID, ...] = ()
    active_intentions: tuple[IntentionRecord, ...] = ()
    available_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExperienceRecord:
    """A reconstruction recipe carried by an ``experience_recorded`` Event."""

    source_event_id: uuid.UUID
    situation: dict[str, Any]
    belief_before_action: dict[str, Any]
    intention: dict[str, Any]
    action: dict[str, Any]
    reason: str
    result: dict[str, Any]
    surprise: Any = None
    lesson: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to an Event payload without duplicating source journals."""

        return {
            "source_event_id": str(self.source_event_id),
            "situation": self.situation,
            "belief_before_action": self.belief_before_action,
            "intention": self.intention,
            "action": self.action,
            "reason": self.reason,
            "result": self.result,
            "surprise": self.surprise,
            "lesson": self.lesson,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperienceRecord":
        """Restore an Experience recipe from an Event payload."""

        return cls(
            source_event_id=uuid.UUID(str(data["source_event_id"])),
            situation=dict(data.get("situation") or {}),
            belief_before_action=dict(data.get("belief_before_action") or {}),
            intention=dict(data.get("intention") or {}),
            action=dict(data.get("action") or {}),
            reason=str(data.get("reason") or ""),
            result=dict(data.get("result") or {}),
            surprise=data.get("surprise"),
            lesson=data.get("lesson"),
        )
