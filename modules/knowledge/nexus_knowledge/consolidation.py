"""Memory Consolidation (Phase K3).

Not summarisation.  A :class:`Consolidator` finds related Knowledge, orders it
in time, compresses duplicates, keeps contradictions apart, separates old
belief from new, and names what is still unresolved — then writes the result
back to the same Knowledge Ledger as an ordinary revision, ``kind=
KIND_CONSOLIDATED_MEMORY``.  The sources it consolidated are never deleted
(spec §8: "ただし元Knowledgeは削除しない").

Consolidated Memory can itself be consolidated again (spec §9) — the
``generation`` in each revision's ``metadata`` and the idempotent
"nothing new since last time" check are what stop that recursion from
looping forever, not a hard-coded ceiling on how deep meaning may compress.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from nexus_knowledge._support.backends.base import BackendRequest
from nexus_knowledge._support.core.event import utcnow
from .ledger import KnowledgeLedger
from .models import (
    KIND_CONSOLIDATED_MEMORY,
    RELATION_ABOUT,
    STATUS_CANDIDATE,
    KnowledgeRevision,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nexus_knowledge._support.backends.base import ExecutionBackend

#: A consolidation more than this many generations deep is refused outright —
#: the hard bound backing the idempotent "nothing new" check (spec §9,
#: "無限要約ループを防ぐ").
DEFAULT_MAX_GENERATION = 8

CONSOLIDATION_INSTRUCTION = (
    "You consolidate several related pieces of raw knowledge into one "
    "coherent memory for long-term recall.\n"
    "- Put events in temporal order and compress duplicates.\n"
    "- Note state transitions: what was believed first, what is believed now.\n"
    "- If sources disagree, describe the disagreement — never silently pick "
    "one side or state a conclusion the inputs do not actually support.\n"
    "- List anything still unresolved or open (a hypothesis, a question, a "
    "conflict) in `unresolved`; leave it empty only if genuinely nothing is "
    "open.\n"
    "Answer with JSON only."
)

CONSOLIDATION_SCHEMA = {
    "type": "object",
    "required": ["summary", "confidence"],
    "properties": {
        "summary": {"type": "string"},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}


def select_candidates(
    ledger: KnowledgeLedger,
    *,
    about: str | None = None,
    kinds: tuple[str, ...] = ("raw", "consolidated_memory", "experience"),
    since: datetime | None = None,
    until: datetime | None = None,
    max_items: int = 20,
) -> list[KnowledgeRevision]:
    """Bounded, deterministic candidate selection (spec §21).

    No embeddings, no brute-force LLM scan over the whole Ledger — just the
    cheap structural signals: relation to a shared subject, a time window,
    and which kinds are eligible at all.  ``max_items`` bounds how much a
    single consolidation call is ever asked to read.
    """
    pool = [h for h in ledger.all_heads() if h.kind in kinds]
    if about is not None:
        pool = [
            h for h in pool if any(r.type == RELATION_ABOUT and r.target == about for r in h.relations)
        ]
    if since is not None:
        pool = [h for h in pool if h.recorded_at >= since]
    if until is not None:
        pool = [h for h in pool if h.recorded_at <= until]
    pool.sort(key=lambda r: r.recorded_at)
    return pool[:max_items]


class Consolidator:
    """Turns a candidate set into one Consolidated Memory Knowledge object."""

    def __init__(
        self,
        ledger: KnowledgeLedger,
        backend: "ExecutionBackend | None" = None,
        *,
        max_generation: int = DEFAULT_MAX_GENERATION,
    ) -> None:
        self.ledger = ledger
        self.backend = backend
        self.max_generation = max_generation

    async def consolidate(
        self,
        candidates: list[KnowledgeRevision],
        *,
        about: str | None = None,
        force: bool = False,
    ) -> KnowledgeRevision | None:
        """Consolidate ``candidates`` into one memory, or reuse a fresh one.

        Returns ``None`` for a set too small to say anything about (fewer
        than two items) or too deep to keep recursing on (bounded execution).
        """
        if len(candidates) < 2:
            return None
        generation = 1 + max((c.metadata.get("generation", 0) for c in candidates), default=0)
        if generation > self.max_generation:
            return None

        derived_ids = sorted({c.knowledge_id for c in candidates})
        if not force:
            existing = self._find_fresh_existing(derived_ids, candidates)
            if existing is not None:
                return existing

        summary, unresolved, confidence = await self._summarize(candidates)

        return self.ledger.record(
            summary,
            source_type="consolidation",
            kind=KIND_CONSOLIDATED_MEMORY,
            derived_from=derived_ids,
            status=STATUS_CANDIDATE if unresolved else None,
            relations=[_about_relation(about)] if about else [],
            metadata={
                "generation": generation,
                "unresolved": unresolved,
                "confidence": confidence,
                "about": about,
                "last_consolidated_at": utcnow().isoformat(),
            },
        )

    # --- idempotency -----------------------------------------------------

    def _find_fresh_existing(
        self, derived_ids: list[str], candidates: list[KnowledgeRevision]
    ) -> KnowledgeRevision | None:
        """A prior consolidation of exactly this set, still up to date.

        "Up to date" means no source has been revised since it ran — the
        guard against re-consolidating unchanged input into an endless chain
        of identical memories (spec §9: idempotency, novelty).
        """
        latest_source_change = max((c.recorded_at for c in candidates), default=utcnow())
        for cm in self.ledger.by_kind(KIND_CONSOLIDATED_MEMORY):
            if sorted(cm.derived_from) != derived_ids:
                continue
            last = cm.metadata.get("last_consolidated_at")
            if last and datetime.fromisoformat(last) >= latest_source_change:
                return cm
        return None

    # --- LLM call + deterministic fallback --------------------------------

    async def _summarize(
        self, candidates: list[KnowledgeRevision]
    ) -> tuple[str, list[str], float]:
        if self.backend is None:
            return self._fallback(candidates)
        request = BackendRequest(
            instruction=CONSOLIDATION_INSTRUCTION,
            context={"items": [self._item_context(c) for c in candidates]},
            output_schema=CONSOLIDATION_SCHEMA,
            metadata={"kind": "consolidation", "item_count": len(candidates)},
        )
        try:
            result = await self.backend.execute(request)
        except Exception:  # noqa: BLE001 - a raising backend still needs an answer
            return self._fallback(candidates)
        if not result.success or not isinstance(result.parsed_output, dict):
            return self._fallback(candidates)
        data = result.parsed_output
        summary = data.get("summary")
        if not summary or not isinstance(summary, str):
            return self._fallback(candidates)
        unresolved = [str(u) for u in (data.get("unresolved") or [])]
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return summary, unresolved, confidence

    @staticmethod
    def _item_context(rev: KnowledgeRevision) -> dict:
        return {
            "knowledge_id": rev.knowledge_id,
            "content": rev.content.value,
            "source": rev.source.to_dict(),
            "recorded_at": rev.recorded_at.isoformat(),
            "kind": rev.kind,
            "status": rev.status,
        }

    @staticmethod
    def _fallback(candidates: list[KnowledgeRevision]) -> tuple[str, list[str], float]:
        """No backend (or an unusable answer): list the raw items, verbatim.

        The one thing a fallback must never do is invent a resolved
        conclusion the inputs do not support (spec: "「CD variationが原因」
        と誤って確定してはいけない") — so it deliberately does not synthesize
        anything, only orders and lists what it was given.
        """
        lines = [
            f"- {c.content.value} (source: {c.source.type}:{c.source.ref or '-'}, "
            f"{c.recorded_at.date().isoformat()})"
            for c in sorted(candidates, key=lambda c: c.recorded_at)
        ]
        summary = "未統合の関連Knowledge（要約なし・原文一覧）:\n" + "\n".join(lines)
        return summary, ["consolidation backend unavailable; listed without synthesis"], 0.0


def _about_relation(target: str):
    from .models import Relation

    return Relation(type=RELATION_ABOUT, target=target)
