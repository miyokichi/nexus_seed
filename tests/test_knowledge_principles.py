"""Phase K4 — Principle Extraction: candidates, counterexamples, prediction feedback."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge import (
    CounterexampleSearcher,
    KnowledgeLedger,
    PredictionEngine,
    PrincipleExtractor,
    apply_prediction_feedback,
    evaluate_prediction,
    record_support,
    refine_principle,
)
from nexus_seed.knowledge.models import STATUS_CANDIDATE, STATUS_REFINED, STATUS_SUPPORTED, STATUS_VALIDATED
from nexus_seed.knowledge.principles import PREDICTION_CONFIRMED, PREDICTION_DISCONFIRMED
from nexus_seed.knowledge.projection import WorldStateProjection, annotate_world_fact
from nexus_seed.storage import Database, KnowledgeStore


def _ledger(tmp_path) -> KnowledgeLedger:
    db = Database(tmp_path / "k.db")
    return KnowledgeLedger(KnowledgeStore(db))


# --- extraction from multiple cases -----------------------------------------


async def test_extraction_generalises_from_multiple_cases(tmp_path):
    """The exact spec §14 example: three projects -> one candidate principle."""
    ledger = _ledger(tmp_path)
    case_a = ledger.record("Project A: early freeze -> large rework", source_type="experience")
    case_b = ledger.record("Project B: early freeze -> large rework", source_type="experience")
    case_c = ledger.record("Project C: validation後freeze -> low rework", source_type="experience")

    principle_text = (
        "不確実性が高い状態で不可逆な仕様固定を行うと、後工程でのrework riskが高まりやすい。"
    )
    backend = FakeLLMBackend(
        script=[proposal_response({"principle": principle_text, "confidence": 0.6, "scope": None})]
    )
    extractor = PrincipleExtractor(ledger, backend)
    principle = await extractor.extract([case_a, case_b, case_c], subject="freeze_timing")

    assert principle is not None
    assert principle.kind == "principle"
    assert principle.status == STATUS_CANDIDATE
    assert principle.content.value == principle_text
    assert sorted(principle.derived_from) == sorted(
        [case_a.knowledge_id, case_b.knowledge_id, case_c.knowledge_id]
    )
    assert principle.metadata["support_count"] == 0


async def test_extraction_needs_at_least_two_cases(tmp_path):
    ledger = _ledger(tmp_path)
    case = ledger.record("solo case", source_type="experience")
    extractor = PrincipleExtractor(ledger, backend=None)
    assert await extractor.extract([case]) is None


async def test_extraction_without_backend_never_claims_causality(tmp_path):
    ledger = _ledger(tmp_path)
    a = ledger.record("case a", source_type="experience")
    b = ledger.record("case b", source_type="experience")
    extractor = PrincipleExtractor(ledger, backend=None)
    principle = await extractor.extract([a, b])
    assert principle.metadata["confidence"] == 0.0
    assert principle.status == STATUS_CANDIDATE


# --- counterexample search refines / downgrades a principle ----------------


async def test_counterexample_refines_and_narrows_scope(tmp_path):
    """spec §17: variation -> failure, refined to low-margin AND variation -> failure risk."""
    ledger = _ledger(tmp_path)
    c1 = ledger.record("variation increases -> failure", source_type="experience")
    c2 = ledger.record("variation increases -> failure", source_type="experience")
    backend = FakeLLMBackend(
        script=[proposal_response({"principle": "variation increases -> failure", "confidence": 0.7, "scope": None})]
    )
    extractor = PrincipleExtractor(ledger, backend)
    principle = await extractor.extract([c1, c2])

    counter_case = ledger.record(
        "variation increases, margin is large -> success", source_type="experience"
    )
    search_backend = FakeLLMBackend(
        script=[proposal_response({"is_counterexample": True, "reason": "large margin absorbed the variation"})]
    )
    searcher = CounterexampleSearcher(search_backend)
    found = await searcher.search(principle, pool=[counter_case])
    assert found == [counter_case]

    refined = refine_principle(
        ledger,
        principle,
        found,
        refined_text="low margin AND variation increases -> failure risk increases",
    )
    assert refined.status == STATUS_REFINED
    assert refined.content.value == "low margin AND variation increases -> failure risk increases"
    assert counter_case.knowledge_id in refined.metadata["evidence_against"]
    assert refined.metadata["counterexample_count"] == 1

    # The original candidate revision is still in history, untouched.
    history = ledger.history(principle.knowledge_id)
    assert history[0].content.value == "variation increases -> failure"


async def test_counterexample_search_never_fabricates_without_backend(tmp_path):
    ledger = _ledger(tmp_path)
    c1 = ledger.record("x -> y", source_type="experience")
    c2 = ledger.record("x -> y", source_type="experience")
    principle = await PrincipleExtractor(ledger, FakeLLMBackend(
        script=[proposal_response({"principle": "x -> y", "confidence": 0.5, "scope": None})]
    )).extract([c1, c2])

    other = ledger.record("some unrelated case", source_type="experience")
    searcher = CounterexampleSearcher(backend=None)
    assert await searcher.search(principle, pool=[other]) == []


async def test_counterexample_downgrades_a_matured_principle(tmp_path):
    ledger = _ledger(tmp_path)
    c1 = ledger.record("x -> y", source_type="experience")
    c2 = ledger.record("x -> y", source_type="experience")
    principle = await PrincipleExtractor(ledger, FakeLLMBackend(
        script=[proposal_response({"principle": "x -> y", "confidence": 0.5, "scope": None})]
    )).extract([c1, c2])
    principle = record_support(ledger, principle, "evidence-1")
    principle = record_support(ledger, principle, "evidence-2")
    assert principle.status == STATUS_SUPPORTED

    counter = ledger.record("x but not y", source_type="experience")
    refined = refine_principle(ledger, principle, [counter])
    assert refined.status == STATUS_REFINED  # demoted, not left at "supported"


# --- prediction generation + evaluation feedback ----------------------------


async def test_prediction_applies_principle_to_current_view(tmp_path):
    ledger = _ledger(tmp_path)
    c1 = ledger.record("high uncertainty + irreversible decision -> rework", source_type="experience")
    c2 = ledger.record("high uncertainty + irreversible decision -> rework", source_type="experience")
    principle = await PrincipleExtractor(ledger, FakeLLMBackend(
        script=[proposal_response({
            "principle": "high uncertainty + low reversibility -> rework risk increases",
            "confidence": 0.6, "scope": None,
        })]
    )).extract([c1, c2])

    raw = ledger.record("project-B is about to freeze the spec under high uncertainty", source_type="report")
    annotate_world_fact(ledger, raw.knowledge_id, entity="project-B", attribute="uncertainty", value="high")
    view = WorldStateProjection(ledger).view()

    predict_backend = FakeLLMBackend(
        script=[proposal_response({
            "predicted_outcome": "project-B is likely to see rework risk increase",
            "confidence": 0.65,
            "predicted_state": [{"entity": "project-B", "attribute": "rework_risk", "value": "high"}],
        })]
    )
    engine = PredictionEngine(ledger, predict_backend)
    prediction = await engine.predict(principle, view, subject="project-B")

    assert prediction.kind == "prediction"
    assert prediction.derived_from == [principle.knowledge_id]
    assert prediction.metadata["predicted_state"] == [
        {"entity": "project-B", "attribute": "rework_risk", "value": "high"}
    ]
    assert prediction.metadata["evaluated"] is False


async def test_prediction_evaluated_against_actual_outcome_confirms_principle(tmp_path):
    ledger = _ledger(tmp_path)
    c1 = ledger.record("x -> y", source_type="experience")
    c2 = ledger.record("x -> y", source_type="experience")
    principle = await PrincipleExtractor(ledger, FakeLLMBackend(
        script=[proposal_response({"principle": "x -> y", "confidence": 0.5, "scope": None})]
    )).extract([c1, c2])

    predict_backend = FakeLLMBackend(
        script=[proposal_response({
            "predicted_outcome": "rework risk increases",
            "confidence": 0.6,
            "predicted_state": [{"entity": "project-C", "attribute": "rework_risk", "value": "high"}],
        })]
    )
    view = WorldStateProjection(ledger).view()
    prediction = await PredictionEngine(ledger, predict_backend).predict(principle, view, subject="project-C")

    actual = {"project-C": {"rework_risk": "high"}}  # what really happened
    updated_prediction, matched = evaluate_prediction(ledger, prediction, actual)
    assert matched is True
    assert updated_prediction.status == PREDICTION_CONFIRMED

    updated_principle = apply_prediction_feedback(ledger, principle, updated_prediction, matched)
    assert updated_principle.metadata["support_count"] == 1
    assert prediction.knowledge_id in updated_principle.metadata["evidence_for"]


async def test_prediction_evaluated_against_actual_outcome_refines_principle(tmp_path):
    ledger = _ledger(tmp_path)
    c1 = ledger.record("x -> y", source_type="experience")
    c2 = ledger.record("x -> y", source_type="experience")
    principle = await PrincipleExtractor(ledger, FakeLLMBackend(
        script=[proposal_response({"principle": "x -> y", "confidence": 0.5, "scope": None})]
    )).extract([c1, c2])

    predict_backend = FakeLLMBackend(
        script=[proposal_response({
            "predicted_outcome": "rework risk increases",
            "confidence": 0.6,
            "predicted_state": [{"entity": "project-D", "attribute": "rework_risk", "value": "high"}],
        })]
    )
    view = WorldStateProjection(ledger).view()
    prediction = await PredictionEngine(ledger, predict_backend).predict(principle, view, subject="project-D")

    actual = {"project-D": {"rework_risk": "low"}}  # the prediction did not hold
    updated_prediction, matched = evaluate_prediction(ledger, prediction, actual)
    assert matched is False
    assert updated_prediction.status == PREDICTION_DISCONFIRMED

    updated_principle = apply_prediction_feedback(ledger, principle, updated_prediction, matched)
    assert updated_principle.status == STATUS_REFINED
    assert prediction.knowledge_id in updated_principle.metadata["evidence_against"]


async def test_principle_reaches_validated_after_enough_support(tmp_path):
    ledger = _ledger(tmp_path)
    c1 = ledger.record("x -> y", source_type="experience")
    c2 = ledger.record("x -> y", source_type="experience")
    principle = await PrincipleExtractor(ledger, FakeLLMBackend(
        script=[proposal_response({"principle": "x -> y", "confidence": 0.5, "scope": None})]
    )).extract([c1, c2])

    for i in range(4):
        principle = record_support(ledger, principle, f"evidence-{i}")
    assert principle.status == STATUS_VALIDATED
