"""Goal-oriented planning from world facts and principles.

Planner detects possible work. It returns project proposals as data; NEXUS SEED
chooses whether and where to persist them and when to hand them to Project
Manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexus_planner._support.backends.base import BackendRequest
from nexus_planner._support.policy.project_proposal import (
    ProjectProposalPolicy,
    proposal_id,
)

DETECTION_INSTRUCTION = (
    "You compare the current World View against a set of supported/validated "
    "Principles (reusable predictive patterns already earned through "
    "evidence) and decide whether any of them currently applies to the "
    "present facts, implying a Gap, Risk, Opportunity or Conflict worth "
    "acting on.\n"
    "Only report a finding if a principle's condition plausibly matches a "
    "fact actually present in the World View — never invent a risk with no "
    "connection to either. It is correct to return no signals at all.\n"
    "Phrase `request` as a short, concrete request a person would type to "
    "ask for the work — it becomes a project proposal, so it must stand on "
    "its own without this context.\n"
    "Set `risk` and `read_only` honestly: `read_only` work may inspect or "
    "analyse but must not change an external system. They decide whether the "
    "proposal can proceed automatically or has to wait for a person.\n"
    "Answer with JSON only."
)
DETECTION_SCHEMA = {
    "type": "object",
    "required": ["signals"],
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "description", "request", "confidence"],
                "properties": {
                    "type": {"type": "string", "enum": ["gap", "risk", "opportunity", "conflict"]},
                    "description": {"type": "string"},
                    "request": {"type": "string"},
                    "confidence": {"type": "number"},
                    "principle_id": {"type": ["string", "null"]},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "read_only": {"type": "boolean"},
                },
            },
        }
    },
}


@dataclass(frozen=True, slots=True)
class Principle:
    """A planning input principle supplied by the composition root."""

    id: str
    text: Any
    status: str = ""
    scope: Any = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Principle":
        return cls(
            id=str(data.get("id") or data.get("knowledge_id") or ""),
            text=data.get("text") if "text" in data else data.get("content"),
            status=str(data.get("status") or ""),
            scope=data.get("scope"),
        )

    def to_backend_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "status": self.status, "scope": self.scope}


@dataclass(frozen=True, slots=True)
class PlanningWorldView:
    """World facts and conflicts supplied to the planner."""

    snapshot: dict[str, Any] = field(default_factory=dict)
    conflicts: dict[str, list[Any]] = field(default_factory=dict)

    @classmethod
    def from_any(cls, value: Any) -> "PlanningWorldView":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(snapshot=value, conflicts={})
        snapshot = value.snapshot() if hasattr(value, "snapshot") else {}
        raw_conflicts = getattr(value, "conflicts", {}) or {}
        conflicts = {
            f"{entity}.{attribute}": [getattr(fact, "value", fact) for fact in facts]
            for (entity, attribute), facts in raw_conflicts.items()
        } if isinstance(raw_conflicts, dict) else {}
        return cls(snapshot=snapshot, conflicts=conflicts)


@dataclass(frozen=True, slots=True)
class Signal:
    """One detected Gap / Risk / Opportunity / Conflict."""

    type: str
    description: str
    request: str
    confidence: float
    principle_id: str | None = None
    risk: str = "medium"
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class ProposedWork:
    """Planner-owned work proposal data, not a Project Manager object."""

    id: str
    objective: str
    reason: str
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    risk: str = "medium"
    read_only: bool = False
    expected_artifacts: list[Any] = field(default_factory=list)
    status: str = ""
    policy_reason: str = ""
    signal_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "risk": self.risk,
            "read_only": self.read_only,
            "expected_artifacts": list(self.expected_artifacts),
            "status": self.status,
            "policy_reason": self.policy_reason,
            "signal_type": self.signal_type,
        }


class GapRiskOpportunityDetector:
    """Finds where supplied principles match the supplied world view."""

    def __init__(self, backend: "Any | None" = None) -> None:
        self.backend = backend

    async def detect(
        self,
        view: Any,
        principles: list[Principle | dict[str, Any]],
        *,
        intentions: list[str] | None = None,
    ) -> list[Signal]:
        if self.backend is None or not principles:
            return []
        world = PlanningWorldView.from_any(view)
        normalized = [p if isinstance(p, Principle) else Principle.from_mapping(p) for p in principles]
        context: dict[str, Any] = {
            "world_view": world.snapshot,
            "conflicts": world.conflicts,
            "principles": [p.to_backend_dict() for p in normalized],
            "intentions": intentions or [],
        }
        request = BackendRequest(
            instruction=DETECTION_INSTRUCTION,
            context=context,
            output_schema=DETECTION_SCHEMA,
            metadata={"kind": "gap_risk_opportunity"},
        )
        try:
            result = await self.backend.execute(request)
        except Exception:  # noqa: BLE001
            return []
        if not result.success or not isinstance(result.parsed_output, dict):
            return []
        return self._parse(result.parsed_output.get("signals") or [])

    @staticmethod
    def _parse(raw_signals: Any) -> list[Signal]:
        signals: list[Signal] = []
        if not isinstance(raw_signals, list):
            return signals
        for item in raw_signals:
            if not isinstance(item, dict):
                continue
            type_ = item.get("type")
            request_text = item.get("request")
            if type_ not in ("gap", "risk", "opportunity", "conflict") or not request_text:
                continue
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            risk = str(item.get("risk") or "medium").lower()
            signals.append(
                Signal(
                    type=type_,
                    description=str(item.get("description") or ""),
                    request=str(request_text),
                    confidence=confidence,
                    principle_id=item.get("principle_id"),
                    risk=risk if risk in {"low", "medium", "high"} else "medium",
                    read_only=item.get("read_only") is True,
                )
            )
        return signals


class GoalBridge:
    """Turns planner signals into module-neutral proposed work."""

    def __init__(
        self,
        *,
        policy: "ProjectProposalPolicy | None" = None,
        min_confidence: float = 0.5,
    ) -> None:
        self.policy = policy or ProjectProposalPolicy()
        self.min_confidence = min_confidence

    async def submit(
        self, signals: list[Signal], *, source: str = "planner"
    ) -> list[tuple[Signal, ProposedWork]]:
        """Return proposals for signals at or above the confidence threshold."""
        del source
        filed = []
        seen: set[str] = set()
        for signal in signals:
            if signal.confidence < self.min_confidence:
                continue
            evidence = [signal.principle_id] if signal.principle_id else []
            proposal = {
                "objective": signal.request,
                "reason": signal.description or f"{signal.type} detected from a principle",
                "evidence_ids": evidence,
                "confidence": signal.confidence,
                "risk": signal.risk,
                "read_only": signal.read_only,
                "expected_artifacts": [],
            }
            proposed_id = proposal_id(proposal)
            if proposed_id in seen:
                continue
            seen.add(proposed_id)
            decision = self.policy.decide(proposal)
            filed.append(
                (
                    signal,
                    ProposedWork(
                        id=proposed_id,
                        objective=proposal["objective"],
                        reason=proposal["reason"],
                        evidence_ids=evidence,
                        confidence=signal.confidence,
                        risk=signal.risk,
                        read_only=signal.read_only,
                        status=decision.status,
                        policy_reason=decision.reason,
                        signal_type=signal.type,
                    ),
                )
            )
        return filed


__all__ = [
    "DETECTION_INSTRUCTION",
    "DETECTION_SCHEMA",
    "GapRiskOpportunityDetector",
    "GoalBridge",
    "PlanningWorldView",
    "Principle",
    "ProposedWork",
    "Signal",
]
