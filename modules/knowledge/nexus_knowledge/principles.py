"""Principle Extraction (Phase K4).

A Principle is not truth — it is spec §18's definition taken literally: **a
reusable prediction model compressed out of Knowledge History**, of the shape
``(State, Condition) -> predicted ΔState``.  It matures along one ladder
(pattern -> hypothesis -> candidate -> supported -> validated), tracked as the
``status`` of an ordinary ``kind=KIND_PRINCIPLE`` Knowledge revision — never a
new Core primitive.

Three moving parts:

* :class:`PrincipleExtractor` — turn several related cases into one candidate
  principle, generalised beyond any single instance.
* :class:`CounterexampleSearcher` — look for a case the candidate does *not*
  hold for.  A principle that only ever collects supporting evidence is not
  being tested (spec §17).
* :class:`PredictionEngine` — apply a principle to a current World View to get
  a predicted diff, then :func:`evaluate_prediction` / :func:`apply_prediction_feedback`
  score it against what actually happened and push the result back onto the
  principle's own evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus_knowledge._support.backends.base import BackendRequest
from .models import (
    KIND_PREDICTION,
    KIND_PRINCIPLE,
    RELATION_ABOUT,
    STATUS_CANDIDATE,
    STATUS_REFINED,
    STATUS_SUPPORTED,
    STATUS_VALIDATED,
    KnowledgeRevision,
    Relation,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nexus_knowledge._support.backends.base import ExecutionBackend
    from .ledger import KnowledgeLedger
    from .projection import WorldView

#: How many independent confirmations move a principle up the maturity ladder.
SUPPORT_THRESHOLD = 2
VALIDATE_THRESHOLD = 4

#: Prediction outcome markers (free-form status values — spec §15, not a
#: closed enum on KnowledgeRevision).
PREDICTION_CONFIRMED = "confirmed"
PREDICTION_DISCONFIRMED = "disconfirmed"

PRINCIPLE_INSTRUCTION = (
    "You extract one reusable principle from several related past cases.\n"
    "A principle must generalise beyond any single case — state the pattern "
    "in terms that could apply to a *different* future situation, not a "
    "recap of what happened this time.\n"
    "State it as (state/condition) -> (likely consequence), in plain prose. "
    "Name the scope it applies under if the cases suggest one.\n"
    "Answer with JSON only."
)
PRINCIPLE_SCHEMA = {
    "type": "object",
    "required": ["principle", "confidence"],
    "properties": {
        "principle": {"type": "string"},
        "confidence": {"type": "number"},
        "scope": {"type": ["string", "null"]},
    },
}

COUNTEREXAMPLE_INSTRUCTION = (
    "Given a candidate principle and one case, judge whether the case "
    "contradicts the principle: the condition the principle describes held, "
    "but the outcome it predicts did not follow.\n"
    "A case that is merely unrelated to the principle is NOT a counterexample "
    "— only flag a genuine contradiction.\n"
    "Answer with JSON only."
)
COUNTEREXAMPLE_SCHEMA = {
    "type": "object",
    "required": ["is_counterexample", "reason"],
    "properties": {
        "is_counterexample": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

PREDICTION_INSTRUCTION = (
    "Apply the given principle to the current facts to predict what is "
    "likely to change. If you can name specific entity/attribute/value "
    "changes, list them in `predicted_state`; always give a plain-language "
    "`predicted_outcome` as well.\n"
    "Answer with JSON only."
)
PREDICTION_SCHEMA = {
    "type": "object",
    "required": ["predicted_outcome", "confidence"],
    "properties": {
        "predicted_outcome": {"type": "string"},
        "confidence": {"type": "number"},
        "predicted_state": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "attribute": {"type": "string"},
                    "value": {},
                },
            },
        },
    },
}


class PrincipleExtractor:
    """Generalises several related cases into one candidate principle."""

    def __init__(self, ledger: "KnowledgeLedger", backend: "ExecutionBackend | None" = None) -> None:
        self.ledger = ledger
        self.backend = backend

    async def extract(
        self, cases: list[KnowledgeRevision], *, subject: str | None = None
    ) -> KnowledgeRevision | None:
        """A candidate principle (``status=STATUS_CANDIDATE``), or ``None``
        if there are too few cases to generalise from at all."""
        if len(cases) < 2:
            return None
        text, confidence, scope = await self._propose(cases)
        return self.ledger.record(
            text,
            source_type="principle_extraction",
            kind=KIND_PRINCIPLE,
            derived_from=[c.knowledge_id for c in cases],
            status=STATUS_CANDIDATE,
            relations=[Relation(type=RELATION_ABOUT, target=subject)] if subject else [],
            metadata={
                "confidence": confidence,
                "scope": scope,
                "support_count": 0,
                "counterexample_count": 0,
                "evidence_for": [c.knowledge_id for c in cases],
                "evidence_against": [],
            },
        )

    async def _propose(self, cases: list[KnowledgeRevision]) -> tuple[str, float, str | None]:
        if self.backend is None:
            return self._fallback(cases)
        request = BackendRequest(
            instruction=PRINCIPLE_INSTRUCTION,
            context={"cases": [c.content.value for c in cases]},
            output_schema=PRINCIPLE_SCHEMA,
            metadata={"kind": "principle_extraction", "case_count": len(cases)},
        )
        try:
            result = await self.backend.execute(request)
        except Exception:  # noqa: BLE001
            return self._fallback(cases)
        if not result.success or not isinstance(result.parsed_output, dict):
            return self._fallback(cases)
        data = result.parsed_output
        text = data.get("principle")
        if not text or not isinstance(text, str):
            return self._fallback(cases)
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return text, confidence, data.get("scope")

    @staticmethod
    def _fallback(cases: list[KnowledgeRevision]) -> tuple[str, float, str | None]:
        """No backend: name the observed repetition, claim nothing causal."""
        return (
            f"{len(cases)}件の事例で共通のパターンが観測された（未一般化・要レビュー）。",
            0.0,
            None,
        )


class CounterexampleSearcher:
    """Looks for a case a candidate principle does *not* hold for.

    Without a reasoning backend this deliberately finds nothing — refusing to
    fabricate a counterexample is safer than inventing a false one, and a
    principle simply stays unchallenged until a backend is available (spec
    never requires the Runtime to work without an LLM for this step, unlike
    plan selection).
    """

    def __init__(self, backend: "ExecutionBackend | None" = None) -> None:
        self.backend = backend

    async def search(
        self, principle: KnowledgeRevision, pool: list[KnowledgeRevision]
    ) -> list[KnowledgeRevision]:
        if self.backend is None:
            return []
        found = []
        for case in pool:
            if case.knowledge_id in principle.derived_from:
                continue
            request = BackendRequest(
                instruction=COUNTEREXAMPLE_INSTRUCTION,
                context={"principle": principle.content.value, "case": case.content.value},
                output_schema=COUNTEREXAMPLE_SCHEMA,
                metadata={"kind": "counterexample_search", "principle_id": principle.knowledge_id},
            )
            try:
                result = await self.backend.execute(request)
            except Exception:  # noqa: BLE001
                continue
            if not result.success or not isinstance(result.parsed_output, dict):
                continue
            if result.parsed_output.get("is_counterexample"):
                found.append(case)
        return found


def refine_principle(
    ledger: "KnowledgeLedger",
    principle: KnowledgeRevision,
    counterexamples: list[KnowledgeRevision],
    *,
    refined_text: str | None = None,
) -> KnowledgeRevision:
    """Narrow a principle's scope after a counterexample (spec §17).

    A principle that already matured past ``candidate`` is demoted back to
    :data:`STATUS_REFINED` — a counterexample is real evidence the earlier
    maturity level overclaimed, not something to note quietly while the
    status stays put.
    """
    principle = ledger.head(principle.knowledge_id) or principle
    text = refined_text or (
        f"{principle.content.value}\n"
        f"(scope narrowed: {len(counterexamples)}件の反例により全称適用を撤回)"
    )
    meta = dict(principle.metadata)
    meta["counterexample_count"] = meta.get("counterexample_count", 0) + len(counterexamples)
    meta["evidence_against"] = sorted(
        set(meta.get("evidence_against", [])) | {c.knowledge_id for c in counterexamples}
    )
    return ledger.revise(
        principle.knowledge_id,
        value=text,
        status=STATUS_REFINED,
        metadata=meta,
        reason="refined after counterexample",
    )


def record_support(
    ledger: "KnowledgeLedger", principle: KnowledgeRevision, evidence_knowledge_id: str
) -> KnowledgeRevision:
    """Record one more independent confirmation, promoting maturity if earned."""
    principle = ledger.head(principle.knowledge_id) or principle
    meta = dict(principle.metadata)
    support_count = meta.get("support_count", 0) + 1
    meta["support_count"] = support_count
    meta["evidence_for"] = sorted(set(meta.get("evidence_for", [])) | {evidence_knowledge_id})

    new_status = principle.status
    if support_count >= VALIDATE_THRESHOLD:
        new_status = STATUS_VALIDATED
    elif support_count >= SUPPORT_THRESHOLD:
        new_status = STATUS_SUPPORTED
    return ledger.revise(
        principle.knowledge_id, status=new_status, metadata=meta, reason="prediction confirmed"
    )


class PredictionEngine:
    """Applies a Principle to a current World View to get a predicted diff."""

    def __init__(self, ledger: "KnowledgeLedger", backend: "ExecutionBackend | None" = None) -> None:
        self.ledger = ledger
        self.backend = backend

    async def predict(
        self,
        principle: KnowledgeRevision,
        view: "WorldView",
        *,
        subject: str,
    ) -> KnowledgeRevision | None:
        facts = view.snapshot()
        text, confidence, predicted_state = await self._propose(principle, facts)
        return self.ledger.record(
            text,
            source_type="prediction",
            kind=KIND_PREDICTION,
            derived_from=[principle.knowledge_id],
            status=STATUS_CANDIDATE,
            relations=[Relation(type=RELATION_ABOUT, target=subject)],
            metadata={
                "confidence": confidence,
                "predicted_state": predicted_state,
                "evaluated": False,
            },
        )

    async def _propose(
        self, principle: KnowledgeRevision, facts: dict[str, Any]
    ) -> tuple[str, float, list[dict[str, Any]]]:
        if self.backend is None:
            return (
                f"(prediction backend unavailable; principle '{principle.content.value[:60]}' not applied)",
                0.0,
                [],
            )
        request = BackendRequest(
            instruction=PREDICTION_INSTRUCTION,
            context={"principle": principle.content.value, "current_facts": facts},
            output_schema=PREDICTION_SCHEMA,
            metadata={"kind": "prediction", "principle_id": principle.knowledge_id},
        )
        try:
            result = await self.backend.execute(request)
        except Exception:  # noqa: BLE001
            return (f"prediction backend raised for principle {principle.knowledge_id}", 0.0, [])
        if not result.success or not isinstance(result.parsed_output, dict):
            return (f"prediction backend failed for principle {principle.knowledge_id}", 0.0, [])
        data = result.parsed_output
        outcome = str(data.get("predicted_outcome") or "")
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        predicted_state = data.get("predicted_state") or []
        return outcome, confidence, predicted_state


def evaluate_prediction(
    ledger: "KnowledgeLedger",
    prediction: KnowledgeRevision,
    actual: dict[str, dict[str, Any]],
) -> tuple[KnowledgeRevision, bool]:
    """Deterministically score a structured prediction against an actual
    World View snapshot (``{entity: {attribute: value}}`` — the same shape
    :meth:`WorldView.snapshot` returns).  No LLM needed: once a prediction
    names concrete facts, comparing them to what happened is mechanical.
    """
    predicted_state = prediction.metadata.get("predicted_state") or []
    if not predicted_state:
        raise ValueError(
            "prediction has no structured predicted_state to evaluate mechanically; "
            "score it manually and call apply_prediction_feedback with the result"
        )
    matched = all(
        actual.get(item["entity"], {}).get(item["attribute"]) == item["value"]
        for item in predicted_state
    )
    meta = dict(prediction.metadata)
    meta["evaluated"] = True
    meta["matched"] = matched
    updated = ledger.revise(
        prediction.knowledge_id,
        status=PREDICTION_CONFIRMED if matched else PREDICTION_DISCONFIRMED,
        metadata=meta,
        reason="evaluated against actual outcome",
    )
    return updated, matched


def apply_prediction_feedback(
    ledger: "KnowledgeLedger",
    principle: KnowledgeRevision,
    prediction: KnowledgeRevision,
    matched: bool,
) -> KnowledgeRevision:
    """Push a scored prediction's outcome back onto the principle it came from."""
    if matched:
        return record_support(ledger, principle, prediction.knowledge_id)
    return refine_principle(ledger, principle, [prediction])
