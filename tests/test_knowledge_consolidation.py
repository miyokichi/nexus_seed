"""Phase K3 — Memory Consolidation: candidates, LLM summary, contradictions,
recursive consolidation, idempotency, bounded execution."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge import Consolidator, KnowledgeLedger, select_candidates
from nexus_seed.knowledge.consolidation import DEFAULT_MAX_GENERATION
from nexus_seed.knowledge.models import KIND_CONSOLIDATED_MEMORY, RELATION_ABOUT, Relation
from nexus_seed.storage import Database, KnowledgeStore


def _ledger(tmp_path) -> KnowledgeLedger:
    db = Database(tmp_path / "k.db")
    return KnowledgeLedger(KnowledgeStore(db))


def _record_about(ledger, text, project, *, source_type="meeting", source_ref=None):
    rev = ledger.record(text, source_type=source_type, source_ref=source_ref)
    ledger.relate(rev.knowledge_id, Relation(type=RELATION_ABOUT, target=project))
    return ledger.head(rev.knowledge_id)


# --- candidate selection ----------------------------------------------------


async def test_select_candidates_filters_by_relation_and_kind(tmp_path):
    ledger = _ledger(tmp_path)
    _record_about(ledger, "K1: 原因はCD variationらしい", "project-A")
    _record_about(ledger, "K2: CD variationには変化なし", "project-A")
    ledger.record("project-Aと無関係な話", source_type="chat")  # no relation -> excluded

    candidates = select_candidates(ledger, about="project-A")
    assert len(candidates) == 2
    assert all(any(r.type == RELATION_ABOUT and r.target == "project-A" for r in c.relations) for c in candidates)


async def test_select_candidates_is_bounded(tmp_path):
    ledger = _ledger(tmp_path)
    for i in range(10):
        _record_about(ledger, f"note {i}", "project-A")
    candidates = select_candidates(ledger, about="project-A", max_items=3)
    assert len(candidates) == 3


# --- LLM consolidation -------------------------------------------------------


async def test_consolidation_preserves_root_cause_ambiguity(tmp_path):
    """The exact spec §7 example: never confirm a cause the inputs later undercut."""
    ledger = _ledger(tmp_path)
    k1 = _record_about(ledger, "原因はCD variationらしい", "project-A")
    k2 = _record_about(ledger, "CD variationには変化なし", "project-A")
    k3 = _record_about(ledger, "高さvariationが増えている", "project-A")

    summary_text = (
        "原因はCD variationと当初推定されたが、その後CD variationの変化は確認されなかった。"
        "現在は高さvariationが代替原因候補となっている。root causeは未確定。"
    )
    backend = FakeLLMBackend(
        script=[proposal_response({"summary": summary_text, "unresolved": ["root cause"], "confidence": 0.7})]
    )
    consolidator = Consolidator(ledger, backend)
    candidates = select_candidates(ledger, about="project-A")
    cm = await consolidator.consolidate(candidates, about="project-A")

    assert cm is not None
    assert cm.kind == KIND_CONSOLIDATED_MEMORY
    assert "未確定" in cm.content.value
    assert "CD variationが原因" not in cm.content.value or "未確定" in cm.content.value
    assert sorted(cm.derived_from) == sorted([k1.knowledge_id, k2.knowledge_id, k3.knowledge_id])
    assert cm.metadata["unresolved"] == ["root cause"]

    # Sources are never deleted.
    assert ledger.head(k1.knowledge_id) is not None
    assert ledger.head(k1.knowledge_id).content.value == "原因はCD variationらしい"


async def test_consolidation_without_backend_never_invents_a_conclusion(tmp_path):
    ledger = _ledger(tmp_path)
    k1 = _record_about(ledger, "B案の方がmarginはありそう", "project-A")
    k2 = _record_about(ledger, "process追加が必要でschedule riskが高い", "project-A")

    consolidator = Consolidator(ledger, backend=None)
    candidates = select_candidates(ledger, about="project-A")
    cm = await consolidator.consolidate(candidates, about="project-A")

    assert cm is not None
    assert "B案の方がmarginはありそう" in cm.content.value
    assert "process追加が必要でschedule riskが高い" in cm.content.value
    assert cm.metadata["confidence"] == 0.0
    assert cm.metadata["unresolved"]


async def test_contradictory_knowledge_stays_in_derived_from_not_collapsed(tmp_path):
    ledger = _ledger(tmp_path)
    a = _record_about(ledger, "deadline: Sep 1", "project-A", source_type="email", source_ref="A")
    b = _record_about(ledger, "deadline: Sep 15", "project-A", source_type="email", source_ref="B")
    ledger.mark_conflict(a.knowledge_id, b.knowledge_id, reason="two sources disagree")
    # mark_conflict revised both -> re-fetch current heads for consolidation.
    candidates = [ledger.head(a.knowledge_id), ledger.head(b.knowledge_id)]

    backend = FakeLLMBackend(
        script=[proposal_response({
            "summary": "締切について source A は Sep 1、source B は Sep 15 と主張しており未解決。",
            "unresolved": ["deadline conflict"],
            "confidence": 0.5,
        })]
    )
    consolidator = Consolidator(ledger, backend)
    cm = await consolidator.consolidate(candidates, about="project-A")

    assert "Sep 1" in cm.content.value and "Sep 15" in cm.content.value
    assert sorted(cm.derived_from) == sorted([a.knowledge_id, b.knowledge_id])


# --- recursive consolidation + idempotency + bounded execution -------------


async def test_recursive_consolidation_across_generations(tmp_path):
    ledger = _ledger(tmp_path)
    k1 = _record_about(ledger, "note 1", "project-A")
    k2 = _record_about(ledger, "note 2", "project-A")
    k3 = _record_about(ledger, "note 3", "project-A")
    k4 = _record_about(ledger, "note 4", "project-A")

    backend = FakeLLMBackend(default=proposal_response({"summary": "gen1 summary", "unresolved": [], "confidence": 0.8}))
    consolidator = Consolidator(ledger, backend)

    gen1a = await consolidator.consolidate([k1, k2], about="project-A")
    gen1b = await consolidator.consolidate([k3, k4], about="project-A")
    assert gen1a.metadata["generation"] == 1
    assert gen1b.metadata["generation"] == 1

    backend.script = [proposal_response({"summary": "gen2 summary", "unresolved": [], "confidence": 0.9})]
    gen2 = await consolidator.consolidate([gen1a, gen1b], about="project-A")
    assert gen2 is not None
    assert gen2.metadata["generation"] == 2
    assert sorted(gen2.derived_from) == sorted([gen1a.knowledge_id, gen1b.knowledge_id])


async def test_consolidation_is_idempotent(tmp_path):
    ledger = _ledger(tmp_path)
    k1 = _record_about(ledger, "note 1", "project-A")
    k2 = _record_about(ledger, "note 2", "project-A")
    backend = FakeLLMBackend(default=proposal_response({"summary": "s", "unresolved": [], "confidence": 0.6}))
    consolidator = Consolidator(ledger, backend)

    first = await consolidator.consolidate([k1, k2], about="project-A")
    second = await consolidator.consolidate([k1, k2], about="project-A")

    assert first.id == second.id  # no duplicate written
    assert len(backend.calls) == 1  # second call never even asked the backend


async def test_new_evidence_breaks_idempotency_and_reconsolidates(tmp_path):
    ledger = _ledger(tmp_path)
    k1 = _record_about(ledger, "note 1", "project-A")
    k2 = _record_about(ledger, "note 2", "project-A")
    backend = FakeLLMBackend(default=proposal_response({"summary": "s1", "unresolved": [], "confidence": 0.6}))
    consolidator = Consolidator(ledger, backend)
    first = await consolidator.consolidate([k1, k2], about="project-A")

    k2_revised = ledger.revise(k2.knowledge_id, value="note 2 updated")
    backend.script = [proposal_response({"summary": "s2", "unresolved": [], "confidence": 0.7})]
    second = await consolidator.consolidate([k1, k2_revised], about="project-A")

    assert second.id != first.id
    assert second.content.value == "s2"


async def test_bounded_execution_refuses_beyond_max_generation(tmp_path):
    ledger = _ledger(tmp_path)
    k1 = ledger.record("a", source_type="chat")
    k2 = ledger.record("b", source_type="chat")
    backend = FakeLLMBackend(default=proposal_response({"summary": "s", "unresolved": [], "confidence": 0.5}))
    consolidator = Consolidator(ledger, backend, max_generation=2)

    gen1 = await consolidator.consolidate([k1, k2])
    assert gen1.metadata["generation"] == 1
    # Fabricate a "gen2" source so the next consolidate would be gen3 > max.
    fake_high_gen = ledger.record("c", source_type="chat", kind=KIND_CONSOLIDATED_MEMORY, metadata={"generation": 2})
    result = await consolidator.consolidate([gen1, fake_high_gen])
    assert result is None  # refused: would exceed max_generation


async def test_default_max_generation_is_a_real_bound(tmp_path):
    assert DEFAULT_MAX_GENERATION > 0
