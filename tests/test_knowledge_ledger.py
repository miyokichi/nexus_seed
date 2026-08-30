"""Phase K1 — Knowledge Ledger: revisions, temporal queries, late evidence."""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.knowledge import Annotation, KnowledgeLedger, Relation
from nexus_seed.knowledge.models import RELATION_ABOUT
from nexus_seed.storage import Database, KnowledgeStore


def _ledger(tmp_path) -> KnowledgeLedger:
    db = Database(tmp_path / "k.db")
    return KnowledgeLedger(KnowledgeStore(db))


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# --- Revision: old revisions stay retrievable after an update -------------


async def test_revision_history_keeps_old_versions(tmp_path):
    ledger = _ledger(tmp_path)
    rev1 = ledger.record("A案継続が安全", source_type="meeting", source_ref="review_1")
    rev2 = ledger.revise(rev1.knowledge_id, value="B案再検討の合理性が高まっている")

    assert rev2.revision == 2
    assert rev2.parents == [rev1.id]

    history = ledger.history(rev1.knowledge_id)
    assert [r.revision for r in history] == [1, 2]
    assert history[0].content.value == "A案継続が安全"
    assert history[1].content.value == "B案再検討の合理性が高まっている"

    # HEAD is the latest, but v1 is still directly addressable.
    assert ledger.head(rev1.knowledge_id).content.value == "B案再検討の合理性が高まっている"
    assert ledger.at_revision(rev1.knowledge_id, 1).content.value == "A案継続が安全"


async def test_annotate_and_relate_do_not_rewrite_content(tmp_path):
    ledger = _ledger(tmp_path)
    rev1 = ledger.record("D1のCD variationが増加傾向", source_type="meeting")

    ledger.annotate(
        rev1.knowledge_id,
        Annotation(kind="world_fact", value={"entity": "D1", "attribute": "cd_trend", "value": "up"}),
    )
    ledger.relate(rev1.knowledge_id, Relation(type=RELATION_ABOUT, target="project-A"))

    head = ledger.head(rev1.knowledge_id)
    assert head.revision == 3
    assert head.content.value == "D1のCD variationが増加傾向"  # unchanged
    assert head.annotations[0].kind == "world_fact"
    assert head.relations[0].target == "project-A"


# --- Temporal query: current vs a past point in time ----------------------


async def test_temporal_query_current_and_past_state(tmp_path):
    ledger = _ledger(tmp_path)
    rev1 = ledger.record(
        "margin: sufficient", source_type="meeting", recorded_at=dt("2026-08-10T00:00:00")
    )
    t_after_v1 = dt("2026-08-15T00:00:00")
    ledger.revise(
        rev1.knowledge_id, value="margin: insufficient", recorded_at=dt("2026-08-20T00:00:00")
    )

    # Current state.
    assert ledger.head(rev1.knowledge_id).content.value == "margin: insufficient"

    # What we knew as of a time before the second revision landed.
    past = ledger.as_known_at(rev1.knowledge_id, t_after_v1)
    assert past is not None
    assert past.content.value == "margin: sufficient"

    # Before anything was recorded at all.
    assert ledger.as_known_at(rev1.knowledge_id, dt("2026-08-01T00:00:00")) is None


# --- Late-arriving evidence -------------------------------------------------


async def test_late_arriving_evidence_preserves_valid_time(tmp_path):
    """8/20 に「8/10 から佐藤担当だった」ことを知った、を正しく扱える。"""
    ledger = _ledger(tmp_path)
    rev1 = ledger.record(
        "Project Aの担当者は鈴木",
        source_type="hr_system",
        recorded_at=dt("2026-08-01T00:00:00"),
        valid_from=dt("2026-08-01T00:00:00"),
    )

    corrected = ledger.revise(
        rev1.knowledge_id,
        value="Project Aの担当者は佐藤",
        recorded_at=dt("2026-08-20T00:00:00"),
        valid_from=dt("2026-08-10T00:00:00"),
        reason="late-arriving evidence: owner actually changed 8/10",
    )

    assert corrected.recorded_at == dt("2026-08-20T00:00:00")
    assert corrected.valid_from == dt("2026-08-10T00:00:00")

    # Valid-time query: who was the owner (as best currently known) on 8/12?
    as_of_8_12 = ledger.valid_at(rev1.knowledge_id, dt("2026-08-12T00:00:00"))
    assert as_of_8_12.content.value == "Project Aの担当者は佐藤"

    # Transaction-time query as of 8/15 (before the correction arrived):
    # NEXUS SEED still believed 鈴木 at that point in its own history.
    as_known_8_15 = ledger.as_known_at(rev1.knowledge_id, dt("2026-08-15T00:00:00"))
    assert as_known_8_15.content.value == "Project Aの担当者は鈴木"


async def test_time_independent_knowledge_has_null_valid_range(tmp_path):
    ledger = _ledger(tmp_path)
    rev = ledger.record("水の沸点は100度(1気圧)", source_type="reference")
    assert rev.valid_from is None
    assert rev.valid_to is None


# --- Contradiction is held, not collapsed ----------------------------------


async def test_conflicting_knowledge_is_preserved_not_merged(tmp_path):
    ledger = _ledger(tmp_path)
    a = ledger.record("deadline: Sep 1", source_type="email", source_ref="source_A")
    b = ledger.record("deadline: Sep 15", source_type="email", source_ref="source_B")

    ledger.mark_conflict(a.knowledge_id, b.knowledge_id, reason="two sources disagree")

    head_a = ledger.head(a.knowledge_id)
    head_b = ledger.head(b.knowledge_id)
    assert head_a.content.value == "deadline: Sep 1"
    assert head_b.content.value == "deadline: Sep 15"
    assert head_a.status == "CONFLICT"
    assert head_b.status == "CONFLICT"
    assert head_a.relations[0].target == b.knowledge_id
    assert head_b.relations[0].target == a.knowledge_id


# --- Query by relation / source / kind --------------------------------------


async def test_query_by_relation_source_and_kind(tmp_path):
    ledger = _ledger(tmp_path)
    k1 = ledger.record("B案はmargin面で有利", source_type="meeting", source_ref="review_1")
    ledger.relate(k1.knowledge_id, Relation(type=RELATION_ABOUT, target="project-A"))
    k2 = ledger.record("A案のDRC riskが顕在化", source_type="report", source_ref="qa_report")
    ledger.relate(k2.knowledge_id, Relation(type=RELATION_ABOUT, target="project-A"))
    ledger.record("無関係な話題", source_type="chat")

    about_a = ledger.referencing("project-A")
    assert {r.knowledge_id for r in about_a} == {k1.knowledge_id, k2.knowledge_id}

    by_source = ledger.by_source("meeting", "review_1")
    assert len(by_source) == 1 and by_source[0].knowledge_id == k1.knowledge_id

    assert all(r.kind == "raw" for r in ledger.by_kind("raw"))
