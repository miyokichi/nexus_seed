"""Goal integration (Phase K5).

Knowledge Runtime never queues work directly (spec §20: "直接Taskを乱発しな
い").  Instead:

    Current View x Relevant Principles x Intentions
        -> Gap / Risk / Opportunity / Conflict  (:class:`Signal`)
        -> the *existing*, unmodified ProjectRouter / ProjectOrchestrator
           entry point (``orchestrator.submit``)

The bridge only ever calls the Project Orchestrator's own public API — it
does not touch ``orchestrator/`` internals, so nothing here can break the
existing routing/escalation test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..backends.base import BackendRequest
from .models import RELATION_ABOUT, KnowledgeRevision, Relation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..backends.base import ExecutionBackend
    from ..orchestrator.orchestrator import ProjectOrchestrator
    from .ledger import KnowledgeLedger
    from .projection import WorldView

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
    "ask for the work — it becomes a Goal candidate handed to the existing "
    "project router, so it must stand on its own without this context.\n"
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
                },
            },
        }
    },
}


@dataclass
class Signal:
    """One detected Gap / Risk / Opportunity / Conflict."""

    type: str
    description: str
    request: str
    confidence: float
    principle_id: str | None = None


class GapRiskOpportunityDetector:
    """Finds where a supported Principle's condition matches the current View.

    Without a reasoning backend this returns nothing — the same conservative
    default as :class:`~nexus_seed.knowledge.principles.CounterexampleSearcher`:
    fabricating a business risk is worse than reporting none.
    """

    def __init__(self, backend: "ExecutionBackend | None" = None) -> None:
        self.backend = backend

    async def detect(
        self,
        view: "WorldView",
        principles: list[KnowledgeRevision],
        *,
        intentions: list[str] | None = None,
    ) -> list[Signal]:
        if self.backend is None or not principles:
            return []
        context: dict[str, Any] = {
            "world_view": view.snapshot(),
            "conflicts": {
                f"{entity}.{attribute}": [f.value for f in facts]
                for (entity, attribute), facts in view.conflicts.items()
            },
            "principles": [
                {
                    "id": p.knowledge_id,
                    "text": p.content.value,
                    "status": p.status,
                    "scope": p.metadata.get("scope"),
                }
                for p in principles
            ],
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
            signals.append(
                Signal(
                    type=type_,
                    description=str(item.get("description") or ""),
                    request=str(request_text),
                    confidence=confidence,
                    principle_id=item.get("principle_id"),
                )
            )
        return signals


class GoalBridge:
    """Submits detected Signals to the existing, unmodified Project Orchestrator.

    Every submission is also recorded back onto the Knowledge Ledger
    (``kind="signal"``), so the causal chain
    Principle -> Signal -> Project stays queryable from the Ledger itself,
    closing the loop back to "what did this Knowledge lead us to do".
    """

    def __init__(
        self,
        ledger: "KnowledgeLedger",
        orchestrator: "ProjectOrchestrator",
        *,
        min_confidence: float = 0.5,
    ) -> None:
        self.ledger = ledger
        self.orchestrator = orchestrator
        self.min_confidence = min_confidence

    async def submit(
        self, signals: list[Signal], *, source: str = "knowledge_runtime"
    ) -> list[tuple[Signal, Any, Any]]:
        """Submit every signal at or above :attr:`min_confidence`.

        Returns ``(signal, RoutingDecision, Project | None)`` triples, in the
        same order the signals were given.
        """
        results = []
        for signal in signals:
            if signal.confidence < self.min_confidence:
                continue
            decision, project = await self.orchestrator.submit(signal.request, source=source)
            self._record_outcome(signal, decision, project)
            results.append((signal, decision, project))
        return results

    def _record_outcome(self, signal: Signal, decision: Any, project: Any) -> KnowledgeRevision:
        relations = [Relation(type=RELATION_ABOUT, target=signal.principle_id)] if signal.principle_id else []
        summary = f"[{signal.type}] {signal.description} -> {decision.action.value}"
        if project is not None:
            summary += f" ({project.id})"
        return self.ledger.record(
            summary,
            source_type="knowledge_runtime_signal",
            kind="signal",
            derived_from=[signal.principle_id] if signal.principle_id else [],
            relations=relations,
            metadata={
                "signal_type": signal.type,
                "request": signal.request,
                "confidence": signal.confidence,
                "routing_action": decision.action.value,
                "project_id": project.id if project is not None else None,
            },
        )
