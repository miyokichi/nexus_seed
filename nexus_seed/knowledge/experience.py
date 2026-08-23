"""Experience -> Principle feedback (Phase K6, light self-learning).

Spec §19/§23: self-improvement goes through
``Experience -> Consolidation -> Principle -> Decision improvement`` — never
through the Runtime rewriting its own code.  This module supplies only the
first and last of those steps; Consolidation and Principle Extraction are the
same :class:`~nexus_seed.knowledge.consolidation.Consolidator` and
:class:`~nexus_seed.knowledge.principles.PrincipleExtractor` from K3/K4 (an
Experience is already one of the eligible ``kinds`` in
:func:`~nexus_seed.knowledge.consolidation.select_candidates`), applied here
to execution history instead of project narrative.

Per-work strategy selection is an Agent Runtime concern, while Knowledge
Runtime sits above both Project Orchestrator and Agent Runtime. This module
therefore never writes to execution components; it only produces
:class:`DecisionAdvisory` values a caller may use in its own selection logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ledger import KnowledgeLedger
from .models import (
    KIND_EXPERIENCE,
    RELATION_ABOUT,
    STATUS_SUPPORTED,
    STATUS_VALIDATED,
    KnowledgeRevision,
    Relation,
)


def record_agent_experience(
    ledger: KnowledgeLedger,
    *,
    agent_id: str,
    task: str,
    outcome: str,
    attempts: list[dict[str, Any]] | None = None,
    strategy_switches: list[dict[str, Any]] | None = None,
    source_ref: str | None = None,
) -> KnowledgeRevision:
    """Record one execution episode as Experience Knowledge.

    ``attempts``/``strategy_switches`` are kept in ``metadata`` — optional
    structured detail, never a forced schema — while ``content`` stays a
    plain narrative any later reader can re-interpret differently.
    """
    attempts = attempts or []
    switches = strategy_switches or []
    narrative = f"{agent_id}: {task} -> {outcome}"
    if switches:
        described = "; ".join(
            f"{s.get('from', '?')} で{s.get('reason', '不調')}が続き {s.get('to', '?')} に切替"
            for s in switches
        )
        narrative += f"（{described}）"
    return ledger.record(
        narrative,
        source_type="agent_execution",
        source_ref=source_ref,
        kind=KIND_EXPERIENCE,
        relations=[Relation(type=RELATION_ABOUT, target=agent_id)],
        metadata={
            "agent_id": agent_id,
            "task": task,
            "outcome": outcome,
            "attempts": attempts,
            "strategy_switches": switches,
        },
    )


@dataclass
class DecisionAdvisory:
    """A plain, mechanism-agnostic hint derived from one mature Principle.

    Deliberately not shaped like any specific Agent Runtime preference model.
    Knowledge Runtime stays decoupled from execution-selection mechanics; a
    caller maps this onto whatever preference structure it actually uses.
    """

    principle_id: str
    principle_text: str
    status: str
    confidence: float
    recommendation: str


def advisories_for(
    principles: list[KnowledgeRevision], *, subject: str | None = None
) -> list[DecisionAdvisory]:
    """Surface mature (supported/validated) Principles as plain advisories.

    A ``candidate`` principle has not yet survived a counterexample check
    (spec §17) and must not steer a live decision — only status that already
    passed :data:`~nexus_seed.knowledge.principles.SUPPORT_THRESHOLD` worth of
    confirmation is exposed here.
    """
    advisories = []
    for principle in principles:
        if principle.status not in (STATUS_SUPPORTED, STATUS_VALIDATED):
            continue
        if subject is not None and not any(
            r.type == RELATION_ABOUT and r.target == subject for r in principle.relations
        ):
            continue
        advisories.append(
            DecisionAdvisory(
                principle_id=principle.knowledge_id,
                principle_text=principle.content.value,
                status=principle.status,
                confidence=float(principle.metadata.get("confidence", 0.0)),
                recommendation=principle.content.value,
            )
        )
    return advisories
