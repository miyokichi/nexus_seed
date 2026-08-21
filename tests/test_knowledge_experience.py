"""Phase K6 — Experience -> Consolidation -> Principle -> decision advisory
(light self-learning; no code self-rewriting, no Agent Runtime coupling)."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge import (
    Consolidator,
    KnowledgeLedger,
    PrincipleExtractor,
    advisories_for,
    record_agent_experience,
    select_candidates,
)
from nexus_seed.knowledge.models import STATUS_CANDIDATE, STATUS_SUPPORTED
from nexus_seed.knowledge.principles import record_support
from nexus_seed.storage import Database, KnowledgeStore


def _ledger(tmp_path) -> KnowledgeLedger:
    db = Database(tmp_path / "k.db")
    return KnowledgeLedger(KnowledgeStore(db))


# --- recording ---------------------------------------------------------------


async def test_record_agent_experience_captures_strategy_switch(tmp_path):
    """spec §19: Agent A repeats the same tool error, switches to Agent B, succeeds."""
    ledger = _ledger(tmp_path)
    rev = record_agent_experience(
        ledger,
        agent_id="agent-A",
        task="resistance_check for D1",
        outcome="failed after 5 identical tool errors",
        attempts=[{"tool": "measure", "error": "ENOTFOUND"} for _ in range(5)],
        strategy_switches=[{"from": "agent-A", "to": "agent-B", "reason": "同一errorが反復"}],
    )
    assert rev.kind == "experience"
    assert "agent-B" in rev.content.value
    assert rev.metadata["strategy_switches"][0]["to"] == "agent-B"
    assert len(rev.metadata["attempts"]) == 5


# --- experience -> consolidation -> principle --------------------------------


async def test_experience_consolidates_and_extracts_a_reusable_principle(tmp_path):
    ledger = _ledger(tmp_path)
    for i in range(3):
        record_agent_experience(
            ledger,
            agent_id=f"agent-{i}",
            task=f"task {i}",
            outcome="failed after repeated identical tool errors, then switched agent and succeeded",
            attempts=[{"tool": "x", "error": "same_error"} for _ in range(5)],
            strategy_switches=[{"from": f"agent-{i}", "to": f"agent-{i}-backup", "reason": "同一error反復"}],
        )

    experiences = select_candidates(ledger, kinds=("experience",))
    assert len(experiences) == 3

    consolidate_backend = FakeLLMBackend(
        script=[proposal_response({
            "summary": "3件の事例すべてで同一errorの反復後、Agent切替により成功した。",
            "unresolved": [],
            "confidence": 0.7,
        })]
    )
    consolidated = await Consolidator(ledger, consolidate_backend).consolidate(experiences)
    assert consolidated is not None
    assert sorted(consolidated.derived_from) == sorted(e.knowledge_id for e in experiences)

    principle_backend = FakeLLMBackend(
        script=[proposal_response({
            "principle": "同一errorが反復しstate進展がない場合、同一戦略継続よりstrategy/agent変更の成功率が高い。",
            "confidence": 0.65,
            "scope": "tool execution retries",
        })]
    )
    # A single consolidated memory alone can't be "generalised" from (needs >=2
    # cases) — extract directly from the underlying experiences instead, the
    # same K4 mechanism used for any other case set.
    principle = await PrincipleExtractor(ledger, principle_backend).extract(experiences)

    assert principle is not None
    assert principle.status == STATUS_CANDIDATE
    assert "strategy/agent変更" in principle.content.value
    assert sorted(principle.derived_from) == sorted(e.knowledge_id for e in experiences)


# --- advisory surfacing --------------------------------------------------------


async def test_advisories_only_surface_mature_principles(tmp_path):
    ledger = _ledger(tmp_path)
    a = ledger.record("case a", source_type="experience")
    b = ledger.record("case b", source_type="experience")
    backend = FakeLLMBackend(
        script=[proposal_response({
            "principle": "同一errorが反復する場合はagentを切り替えるべき",
            "confidence": 0.6,
            "scope": None,
        })]
    )
    principle = await PrincipleExtractor(ledger, backend).extract([a, b])

    # Still a candidate: not yet advisory-worthy.
    assert advisories_for([principle]) == []

    principle = record_support(ledger, principle, "evidence-1")
    principle = record_support(ledger, principle, "evidence-2")
    assert principle.status == STATUS_SUPPORTED

    advisories = advisories_for([principle])
    assert len(advisories) == 1
    assert advisories[0].principle_id == principle.knowledge_id
    assert advisories[0].status == STATUS_SUPPORTED
    assert "agent" in advisories[0].recommendation


async def test_advisories_filter_by_subject_relation(tmp_path):
    ledger = _ledger(tmp_path)
    from nexus_seed.knowledge.models import KIND_PRINCIPLE, RELATION_ABOUT, Relation

    p1 = ledger.record(
        "principle about agent-A",
        source_type="principle_extraction",
        kind=KIND_PRINCIPLE,
        status=STATUS_SUPPORTED,
        relations=[Relation(type=RELATION_ABOUT, target="agent-A")],
        metadata={"confidence": 0.7},
    )
    p2 = ledger.record(
        "principle about agent-B",
        source_type="principle_extraction",
        kind=KIND_PRINCIPLE,
        status=STATUS_SUPPORTED,
        relations=[Relation(type=RELATION_ABOUT, target="agent-B")],
        metadata={"confidence": 0.7},
    )

    only_a = advisories_for([p1, p2], subject="agent-A")
    assert [a.principle_id for a in only_a] == [p1.knowledge_id]
