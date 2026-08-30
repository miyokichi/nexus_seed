"""Goal integration (Phase K5).

Knowledge Runtime never queues work directly (spec §20: "直接Taskを乱発しな
い").  Instead:

    Current View x Relevant Principles x Intentions
        -> Gap / Risk / Opportunity / Conflict  (:class:`Signal`)
        -> a project proposal on the Knowledge Ledger
        -> the autonomy policy, and a person for anything that is not
           low-risk read-only work
        -> ``ProjectOrchestrator.submit``, run by the loop

That last half is deliberately *not* re-implemented here.  There is one route
from Knowledge to a Project, and this joins it: filing a proposal is all this
module does, and :class:`~nexus_seed.knowledge.autonomous_loop.KnowledgeLoop`
routes it like any other.  What this adds is a different lens — the evaluator
inside the loop reacts to new evidence, while this asks the standing question
"where does a principle we already trust apply to the world as it is now?",
which can be true without anything new having arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...backends.base import BackendRequest
from ...policy.project_proposal import (
    KIND_PROJECT_PROPOSAL,
    ProjectProposalPolicy,
    proposal_id,
)
from ..knowledge.models import RELATION_ABOUT, KnowledgeRevision, Relation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...backends.base import ExecutionBackend
    from ..knowledge.ledger import KnowledgeLedger
    from ..knowledge.projection import WorldView
    from ..project_manager.orchestrator import ProjectOrchestrator

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


@dataclass
class Signal:
    """One detected Gap / Risk / Opportunity / Conflict.

    ``risk`` and ``read_only`` are what the autonomy policy judges, and both
    default to the cautious answer: an unstated finding is treated as work
    that could change something, so it waits for a person.
    """

    type: str
    description: str
    request: str
    confidence: float
    principle_id: str | None = None
    risk: str = "medium"
    read_only: bool = False


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
    """Files detected Signals as project proposals on the Knowledge Ledger.

    There is exactly one route from Knowledge to a Project: a proposal, judged
    by :class:`~nexus_seed.knowledge.autonomous_loop.ProjectProposalPolicy`,
    then routed through ``ProjectOrchestrator.submit`` by the loop.  This
    bridge joins that route rather than running beside it, so a
    principle-driven finding gets the same autonomy gate, the same human
    review for anything that is not low-risk read-only work, and the same
    exactly-once submission as an evidence-driven one.

    What it adds is the *lens*: the evaluator inside the loop reacts to new
    evidence, while this asks the standing question "where does a principle we
    already trust apply to the world as it is now?" — which can be true
    without anything new having arrived.
    """

    def __init__(
        self,
        ledger: "KnowledgeLedger",
        *,
        policy: "ProjectProposalPolicy | None" = None,
        min_confidence: float = 0.5,
    ) -> None:
        self.ledger = ledger
        self.policy = policy or ProjectProposalPolicy()
        self.min_confidence = min_confidence

    async def submit(
        self, signals: list[Signal], *, source: str = "knowledge_runtime"
    ) -> list[tuple[Signal, KnowledgeRevision]]:
        """File every signal at or above :attr:`min_confidence` as a proposal.

        Returns ``(signal, proposal)`` pairs for the ones that were filed.  A
        proposal whose identity already exists is skipped rather than
        duplicated, so re-running over an unchanged world view is a no-op.
        """
        filed = []
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
            knowledge_id = proposal_id(proposal)
            existing = self.ledger.head(knowledge_id)
            if existing is not None:
                continue
            decision = self.policy.decide(proposal)
            relations = (
                [Relation(type=RELATION_ABOUT, target=signal.principle_id)]
                if signal.principle_id
                else []
            )
            filed.append(
                (
                    signal,
                    self.ledger.record(
                        signal.request,
                        knowledge_id=knowledge_id,
                        source_type=source,
                        kind=KIND_PROJECT_PROPOSAL,
                        status=decision.status,
                        derived_from=evidence,
                        relations=relations,
                        metadata={
                            **proposal,
                            "policy_reason": decision.reason,
                            "signal_type": signal.type,
                            "detected_from_principle": signal.principle_id,
                        },
                    ),
                )
            )
        return filed
