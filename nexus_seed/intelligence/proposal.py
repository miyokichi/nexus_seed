"""InterpretationProposal — an AI's *proposed* reading, not yet accepted truth.

An LLM never writes world state.  Its structured output becomes an
:class:`InterpretationProposal`, which only becomes an Observation + StateDelta
after validation and a policy decision (Invariants 16–17).  This keeps LLM
output, Observation, StateDelta and World State strictly distinct.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow


class ProposalDecision(str, Enum):
    """The outcome of validating + policy-checking a proposal."""

    PENDING = "PENDING"
    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"
    MODIFIED = "MODIFIED"


@dataclass
class ProposedStateDelta:
    """A state change *proposed* by an LLM (not yet a committed StateDelta)."""

    entity: str
    attribute: str
    old_value: Any = None
    new_value: Any = None
    unit: str | None = None
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "attribute": self.attribute,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "unit": self.unit,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProposedStateDelta":
        return cls(
            entity=data.get("entity", ""),
            attribute=data.get("attribute", ""),
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
            unit=data.get("unit"),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class InterpretationProposal:
    """An AI-proposed interpretation of a raw event, pending validation."""

    subject: str
    predicate: str
    extracted: dict = field(default_factory=dict)
    proposed_state_deltas: list[ProposedStateDelta] = field(default_factory=list)
    proposed_state_deltas_declared: bool = field(default=False, repr=False, compare=False)
    confidence: float = 0.0
    rationale: str | None = None
    source_event_id: uuid.UUID | None = None
    created_by_process_id: uuid.UUID | None = None
    context_snapshot_id: uuid.UUID | None = None
    llm_invocation_id: uuid.UUID | None = None
    decision: ProposalDecision = ProposalDecision.PENDING
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        """Serialize the proposal body (for the ``proposal_json`` column)."""
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "extracted": self.extracted,
            "proposed_state_deltas": [d.to_dict() for d in self.proposed_state_deltas],
            "confidence": self.confidence,
            "rationale": self.rationale,
        }

    @classmethod
    def from_output(
        cls,
        data: Any,
        *,
        source_event_id: uuid.UUID | None = None,
        created_by_process_id: uuid.UUID | None = None,
    ) -> "InterpretationProposal | None":
        """Build a proposal from backend structured output (``None`` if unusable)."""
        if not isinstance(data, dict):
            return None
        deltas_raw = data.get("proposed_state_deltas")
        deltas_declared = isinstance(deltas_raw, list) and all(
            isinstance(delta, dict) for delta in deltas_raw
        )
        if not deltas_declared:
            deltas_raw = []
        try:
            deltas = [ProposedStateDelta.from_dict(d) for d in deltas_raw if isinstance(d, dict)]
        except Exception:  # noqa: BLE001 - malformed output is a schema failure
            return None
        return cls(
            subject=data.get("subject", ""),
            predicate=data.get("predicate", ""),
            extracted=data.get("extracted", {}) if isinstance(data.get("extracted"), dict) else {},
            proposed_state_deltas=deltas,
            proposed_state_deltas_declared=deltas_declared,
            confidence=_as_float(data.get("confidence")),
            rationale=data.get("rationale"),
            source_event_id=source_event_id,
            created_by_process_id=created_by_process_id,
        )


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0  # out of [0,1] -> fails schema validation
