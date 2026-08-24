"""The half of the loop that re-reads: consolidation, principles, re-assessment.

    evidence -> [consolidate] -> [principle] -> assessment -> proposal

`tests/test_knowledge_autonomous_loop.py` covers evidence reaching a Project.
This file covers what happens *between* those two points once knowledge piles
up: old observations get compressed, a reusable principle is generalised over
the compressed material, and a corrected observation is read again.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge.autonomous_loop import (
    KIND_PROJECT_PROPOSAL,
    KIND_SITUATION_ASSESSMENT,
    KnowledgeLoop,
)
from nexus_seed.knowledge.models import (
    KIND_CONSOLIDATED_MEMORY,
    KIND_PRINCIPLE,
    RELATION_ABOUT,
    STATUS_SUPPORTED,
    Relation,
)
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.runtime.runtime import Runtime


class DispatchBackend:
    """One backend, answering each kind of request the loop actually makes.

    The real LLM sees the same thing: one endpoint, told apart by the
    instruction and schema of each request.
    """

    def __init__(self, *, proposals=None):
        self.calls = []
        self._proposals = proposals if proposals is not None else []

    async def execute(self, request):
        kind = request.metadata.get("kind")
        self.calls.append((kind, request))
        if kind == "consolidation":
            return proposal_response(
                {"summary": "統合された記憶", "unresolved": [], "confidence": 0.7}
            )
        if kind == "principle_extraction":
            return proposal_response(
                {"principle": "期限が近い契約は早めに確認すると手戻りが少ない",
                 "confidence": 0.6, "scope": None}
            )
        if kind == "situation_assessment":
            return proposal_response({"proposals": list(self._proposals)})
        return proposal_response({})

    def calls_of(self, kind):
        return [request for called, request in self.calls if called == kind]


def build(tmp_path, backend, **kwargs):
    runtime = Runtime(tmp_path / "loop.db")
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator, backend=backend, **kwargs)
    return runtime, loop


def record(loop, text, *, about=None, kind="raw", source_type="meeting"):
    item = loop.ledger.record(text, source_type=source_type, kind=kind)
    if about:
        item = loop.ledger.relate(
            item.knowledge_id, Relation(type=RELATION_ABOUT, target=about)
        )
    return item


# --- P1: consolidation runs inside the loop ---------------------------------


async def test_consolidation_fires_once_the_subject_piles_up(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend, consolidation_threshold=3)

    for n in range(2):
        record(loop, f"契約Aについての観測 {n}", about="contract-A")
    await loop.reconcile()
    assert loop.ledger.by_kind(KIND_CONSOLIDATED_MEMORY) == []  # under threshold

    record(loop, "契約Aについての観測 2", about="contract-A")
    result = await loop.reconcile()

    assert result.consolidations == 1
    [memory] = loop.ledger.by_kind(KIND_CONSOLIDATED_MEMORY)
    assert memory.content.value == "統合された記憶"
    assert len(memory.derived_from) == 3
    assert memory.metadata["about"] == "contract-A"
    runtime.close()


async def test_knowledge_with_no_subject_is_still_consolidated(tmp_path):
    """A typed-in note names no subject; it must not pile up unread forever."""
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend, consolidation_threshold=3)

    for n in range(3):
        record(loop, f"手入力のメモ {n}")  # no `about` relation
    result = await loop.reconcile()

    assert result.consolidations == 1
    [memory] = loop.ledger.by_kind(KIND_CONSOLIDATED_MEMORY)
    assert len(memory.derived_from) == 3
    runtime.close()


async def test_consolidation_does_not_repeat_while_nothing_new_arrives(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend, consolidation_threshold=3)
    for n in range(3):
        record(loop, f"観測 {n}", about="contract-A")

    await loop.reconcile()
    consolidation_calls = len(backend.calls_of("consolidation"))
    await loop.reconcile()
    await loop.reconcile()

    assert len(backend.calls_of("consolidation")) == consolidation_calls
    assert len(loop.ledger.by_kind(KIND_CONSOLIDATED_MEMORY)) == 1
    runtime.close()


async def test_consolidation_is_bounded_per_pass(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(
        tmp_path, backend, consolidation_threshold=2, consolidations_per_pass=1
    )
    for subject in ("contract-A", "contract-B", "contract-C"):
        for n in range(2):
            record(loop, f"{subject} 観測 {n}", about=subject)

    await loop.reconcile()

    # Three subjects are ready; one tick compresses one of them.
    assert len(loop.ledger.by_kind(KIND_CONSOLIDATED_MEMORY)) == 1
    runtime.close()


async def test_consolidated_memory_becomes_evidence_for_assessment(tmp_path):
    """A memory is evidence in its own right — that is what makes it useful."""
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend, consolidation_threshold=3)
    for n in range(3):
        record(loop, f"観測 {n}", about="contract-A")

    await loop.reconcile()

    [memory] = loop.ledger.by_kind(KIND_CONSOLIDATED_MEMORY)
    seen = {
        item["knowledge_id"]
        for request in backend.calls_of("situation_assessment")
        for item in request.context["new_evidence"]
    }
    assert memory.knowledge_id in seen
    runtime.close()


# --- P1: principles run inside the loop, and reach the evaluator ------------


async def test_principle_is_extracted_over_consolidated_material(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend, principle_threshold=2)

    # Two consolidated memories are enough to generalise across.
    for n in range(2):
        loop.ledger.record(
            f"過去の統合記憶 {n}",
            source_type="consolidation",
            kind=KIND_CONSOLIDATED_MEMORY,
        )
    result = await loop.reconcile()

    assert result.principles == 1
    [principle] = loop.ledger.by_kind(KIND_PRINCIPLE)
    assert principle.content.value.startswith("期限が近い契約")
    assert principle.status == "candidate"
    assert len(principle.derived_from) == 2
    runtime.close()


async def test_principle_extraction_needs_no_llm_call_on_a_quiet_tick(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend, principle_threshold=2)
    for n in range(2):
        loop.ledger.record(
            f"記憶 {n}", source_type="consolidation", kind=KIND_CONSOLIDATED_MEMORY
        )

    await loop.reconcile()
    extraction_calls = len(backend.calls_of("principle_extraction"))
    await loop.reconcile()
    await loop.reconcile()

    assert len(backend.calls_of("principle_extraction")) == extraction_calls
    assert len(loop.ledger.by_kind(KIND_PRINCIPLE)) == 1
    runtime.close()


async def test_only_mature_principles_reach_the_evaluator(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend)

    candidate = loop.ledger.record(
        "まだ反例検証されていない原則",
        source_type="principle_extraction",
        kind=KIND_PRINCIPLE,
        status="candidate",
    )
    record(loop, "新しい観測")
    await loop.reconcile()

    context = backend.calls_of("situation_assessment")[-1].context
    assert context["principles"] == []  # a candidate never steers a decision

    loop.ledger.revise(candidate.knowledge_id, status=STATUS_SUPPORTED)
    record(loop, "さらに新しい観測")
    await loop.reconcile()

    context = backend.calls_of("situation_assessment")[-1].context
    assert [item["knowledge_id"] for item in context["principles"]] == [
        candidate.knowledge_id
    ]
    assert context["principles"][0]["status"] == STATUS_SUPPORTED
    runtime.close()


async def test_no_backend_means_no_consolidation_and_no_principles(tmp_path):
    """Without a reasoning backend the loop stays quiet rather than guessing."""
    runtime, loop = build(tmp_path, None, consolidation_threshold=2, principle_threshold=2)
    for n in range(4):
        record(loop, f"観測 {n}", about="contract-A")

    result = await loop.reconcile()

    assert result.consolidations == 0
    assert result.principles == 0
    assert loop.ledger.by_kind(KIND_CONSOLIDATED_MEMORY) == []
    assert loop.ledger.by_kind(KIND_PRINCIPLE) == []
    runtime.close()


# --- P2: a corrected observation is read again ------------------------------


async def test_revised_knowledge_is_assessed_again(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend)
    item = record(loop, "契約Aの期限は9月1日")

    await loop.reconcile()
    first = [
        entry["content"]
        for request in backend.calls_of("situation_assessment")
        for entry in request.context["new_evidence"]
    ]
    assert first == ["契約Aの期限は9月1日"]

    # Same object, corrected: this is new information, not a duplicate.
    loop.ledger.revise(item.knowledge_id, value="契約Aの期限は9月15日だった")
    await loop.reconcile()

    latest = backend.calls_of("situation_assessment")[-1]
    assert [entry["content"] for entry in latest.context["new_evidence"]] == [
        "契約Aの期限は9月15日だった"
    ]
    # Both readings are on the record; neither overwrote the other.
    assert len(loop.ledger.by_kind(KIND_SITUATION_ASSESSMENT)) == 2
    runtime.close()


async def test_unchanged_knowledge_is_not_assessed_twice(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend)
    record(loop, "契約Aの期限は9月1日")

    await loop.reconcile()
    calls = len(backend.calls_of("situation_assessment"))
    await loop.reconcile()
    await loop.reconcile()

    assert len(backend.calls_of("situation_assessment")) == calls
    runtime.close()


async def test_legacy_assessment_without_revision_ids_is_honoured(tmp_path):
    """Upgrading a database must not re-assess its whole history at once."""
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend)
    item = record(loop, "既存の観測")

    # An assessment as an earlier version wrote it: knowledge ids only.
    loop.ledger.record(
        {"proposals": []},
        source_type="situation_evaluator",
        format="json",
        kind=KIND_SITUATION_ASSESSMENT,
        derived_from=[item.knowledge_id],
        metadata={"evidence_ids": [item.knowledge_id]},
    )

    await loop.reconcile()

    assert backend.calls_of("situation_assessment") == []
    runtime.close()


async def test_revision_after_a_legacy_assessment_is_read_again(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend)
    item = record(loop, "既存の観測")
    loop.ledger.record(
        {"proposals": []},
        source_type="situation_evaluator",
        format="json",
        kind=KIND_SITUATION_ASSESSMENT,
        derived_from=[item.knowledge_id],
        metadata={"evidence_ids": [item.knowledge_id]},
    )

    loop.ledger.revise(item.knowledge_id, value="訂正された観測")
    await loop.reconcile()

    latest = backend.calls_of("situation_assessment")[-1]
    assert [entry["content"] for entry in latest.context["new_evidence"]] == [
        "訂正された観測"
    ]
    runtime.close()


# --- P3: the ledger-position cursors are a cache, never a queue -------------


async def test_a_restarted_loop_does_not_re_assess_its_history(tmp_path):
    """Cursors live in memory; the recorded assessments are what decide "seen"."""
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend)
    record(loop, "契約Aの期限は9月1日")
    await loop.reconcile()
    assert len(backend.calls_of("situation_assessment")) == 1

    # A fresh loop over the same database: every cursor starts at zero again.
    restarted = KnowledgeLoop(runtime, loop.orchestrator, backend=backend)
    assert restarted._assessment_cursor == 0
    await restarted.reconcile()

    assert len(backend.calls_of("situation_assessment")) == 1
    runtime.close()


async def test_a_restarted_loop_still_reads_a_correction(tmp_path):
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend)
    item = record(loop, "契約Aの期限は9月1日")
    await loop.reconcile()

    loop.ledger.revise(item.knowledge_id, value="契約Aの期限は9月15日だった")
    restarted = KnowledgeLoop(runtime, loop.orchestrator, backend=backend)
    await restarted.reconcile()

    latest = backend.calls_of("situation_assessment")[-1]
    assert [entry["content"] for entry in latest.context["new_evidence"]] == [
        "契約Aの期限は9月15日だった"
    ]
    runtime.close()


async def test_a_quiet_tick_does_not_read_the_whole_ledger(tmp_path):
    """A settled Ledger must not cost more to re-check as it grows.

    This is what makes the loop usable for long-running operation: without it
    every tick re-reads every object ever recorded.
    """
    backend = DispatchBackend()
    runtime, loop = build(tmp_path, backend, assessment_batch_size=10_000)
    for n in range(300):
        record(loop, f"観測 {n}")
    for _ in range(4):
        await loop.reconcile()

    rows = 0
    real_query = runtime.db.query

    def counting_query(sql, params=()):
        nonlocal rows
        result = real_query(sql, params)
        rows += len(result)
        return result

    runtime.db.query = counting_query
    try:
        await loop.reconcile()
    finally:
        runtime.db.query = real_query

    # Well under one row per stored object: the pass looks at what is new,
    # not at all 300 objects.
    assert rows < 100, f"quiet tick read {rows} rows for 300 objects"
    runtime.close()


async def test_reassessment_of_the_same_objective_creates_no_second_proposal(tmp_path):
    """Re-reading may repeat a conclusion; it must not repeat the work."""
    backend = DispatchBackend(
        proposals=[
            {
                "objective": "契約Aを確認する",
                "reason": "期限が近い",
                "evidence_ids": [],
                "confidence": 0.9,
                "risk": "low",
                "read_only": True,
            }
        ]
    )
    runtime, loop = build(tmp_path, backend)
    item = record(loop, "契約Aの期限は9月1日")
    backend._proposals[0]["evidence_ids"] = [item.knowledge_id]

    await loop.reconcile()
    assert len(loop.ledger.by_kind(KIND_PROJECT_PROPOSAL)) == 1

    loop.ledger.revise(item.knowledge_id, value="契約Aの期限は9月1日（確認済み）")
    await loop.reconcile()

    assert len(loop.ledger.by_kind(KIND_PROJECT_PROPOSAL)) == 1
    runtime.close()
