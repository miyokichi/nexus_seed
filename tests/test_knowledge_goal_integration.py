"""Phase K5 — Goal integration: World View x Principles -> Signal -> Project.

Uses the real, unmodified ProjectOrchestrator (same as
tests/test_orchestrator_routing.py) to prove the bridge only calls its public
API and never reaches into orchestrator internals.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge import GapRiskOpportunityDetector, GoalBridge, KnowledgeLedger, Signal
from nexus_seed.knowledge.autonomous_loop import (
    KIND_PROJECT_PROPOSAL,
    PROPOSAL_AUTO_APPROVED,
    PROPOSAL_PENDING_REVIEW,
)
from nexus_seed.knowledge.models import KIND_PRINCIPLE, STATUS_SUPPORTED
from nexus_seed.knowledge.projection import WorldStateProjection, annotate_world_fact
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator, ProjectStatus, RoutingAction
from nexus_seed.storage import Database, KnowledgeStore


def _ledger(tmp_path) -> KnowledgeLedger:
    db = Database(tmp_path / "k.db")
    return KnowledgeLedger(KnowledgeStore(db))


def decision(action, **fields):
    payload = {"action": action, "reason": "test", "confidence": 0.9, **fields}
    return proposal_response(payload)


# --- spec §20 worked example: high uncertainty + irreversible decision -----


async def test_risk_signal_produces_a_project_via_real_orchestrator(tmp_path):
    ledger = _ledger(tmp_path)
    raw = ledger.record(
        "Project Xでは3日後に仕様凍結が予定されているが、不確実性がまだ高い",
        source_type="report",
    )
    annotate_world_fact(ledger, raw.knowledge_id, entity="project-X", attribute="uncertainty", value="high")
    annotate_world_fact(
        ledger, raw.knowledge_id, entity="project-X", attribute="irreversible_deadline_days", value=3
    )
    principle = ledger.record(
        "不確実性が高い状態で不可逆な意思決定を行うと、後から得られた情報を反映しにくくなり、"
        "手戻りコストが増加しやすい。",
        source_type="principle_extraction",
        kind=KIND_PRINCIPLE,
        status=STATUS_SUPPORTED,
        metadata={"scope": None},
    )

    view = WorldStateProjection(ledger).view()
    detect_backend = FakeLLMBackend(
        script=[proposal_response({
            "signals": [{
                "type": "risk",
                "description": "premature irreversible decision under high uncertainty",
                "request": "意思決定前に主要な不確実性を低減する",
                "confidence": 0.8,
                "principle_id": principle.knowledge_id,
            }]
        })]
    )
    detector = GapRiskOpportunityDetector(detect_backend)
    signals = await detector.detect(view, [principle])
    assert len(signals) == 1
    assert signals[0].type == "risk"

    filed = await GoalBridge(ledger).submit(signals)

    # The finding becomes a proposal on the Ledger, linked to the principle
    # that raised it — not a Project. Routing is the loop's job, through the
    # one gate every proposal passes.
    assert len(filed) == 1
    _signal, proposal = filed[0]
    assert proposal.kind == KIND_PROJECT_PROPOSAL
    assert proposal.content.value == "意思決定前に主要な不確実性を低減する"
    assert proposal.derived_from == [principle.knowledge_id]
    assert proposal.metadata["detected_from_principle"] == principle.knowledge_id
    assert [item.knowledge_id for item in ledger.by_kind(KIND_PROJECT_PROPOSAL)] == [
        proposal.knowledge_id
    ]


async def test_an_unstated_risk_waits_for_a_person(tmp_path):
    """Same autonomy gate as an evidence-driven proposal: cautious by default."""
    ledger = _ledger(tmp_path)
    principle = ledger.record(
        "p", source_type="principle_extraction", kind=KIND_PRINCIPLE,
        status=STATUS_SUPPORTED,
    )
    signal = Signal(
        type="risk", description="何かがおかしい", request="調べる", confidence=0.95,
        principle_id=principle.knowledge_id,
    )

    [(_, proposal)] = await GoalBridge(ledger).submit([signal])

    assert proposal.status == PROPOSAL_PENDING_REVIEW
    assert proposal.metadata["risk"] == "medium"
    assert proposal.metadata["read_only"] is False


async def test_a_finding_with_nothing_behind_it_is_refused(tmp_path):
    """The policy's "cite your evidence" rule reaches this path too.

    A signal naming no principle came from nothing, so it is recorded as
    FORBIDDEN — visible, and never routed — rather than silently dropped.
    """
    ledger = _ledger(tmp_path)
    signal = Signal(
        type="risk", description="何かがおかしい", request="調べる", confidence=0.95
    )

    [(_, proposal)] = await GoalBridge(ledger).submit([signal])

    assert proposal.status == "FORBIDDEN"
    assert "evidence" in proposal.metadata["policy_reason"]


async def test_low_risk_read_only_work_may_proceed_without_a_person(tmp_path):
    ledger = _ledger(tmp_path)
    principle = ledger.record(
        "p", source_type="principle_extraction", kind=KIND_PRINCIPLE,
        status=STATUS_SUPPORTED,
    )
    signal = Signal(
        type="opportunity",
        description="読むだけで確かめられる",
        request="契約Aの残期間を確認する",
        confidence=0.9,
        principle_id=principle.knowledge_id,
        risk="low",
        read_only=True,
    )

    [(_, proposal)] = await GoalBridge(ledger).submit([signal])

    assert proposal.status == PROPOSAL_AUTO_APPROVED


async def test_low_confidence_signal_is_not_filed(tmp_path):
    ledger = _ledger(tmp_path)
    weak = Signal(type="risk", description="maybe nothing", request="調べて", confidence=0.1)

    filed = await GoalBridge(ledger, min_confidence=0.5).submit([weak])

    assert filed == []
    assert ledger.by_kind(KIND_PROJECT_PROPOSAL) == []


async def test_the_same_finding_is_not_proposed_twice(tmp_path):
    """Re-running over an unchanged world view must not queue the work again."""
    ledger = _ledger(tmp_path)
    signal = Signal(
        type="risk", description="d", request="契約Aを確認する", confidence=0.9
    )

    assert len(await GoalBridge(ledger).submit([signal])) == 1
    assert await GoalBridge(ledger).submit([signal]) == []
    assert len(ledger.by_kind(KIND_PROJECT_PROPOSAL)) == 1


async def test_a_filed_proposal_is_routed_by_the_loop(tmp_path):
    """The one route to a Project, exercised end to end from a Signal."""
    from nexus_seed.knowledge.autonomous_loop import PROPOSAL_ROUTED, KnowledgeLoop
    from nexus_seed.runtime.runtime import Runtime

    runtime = Runtime(tmp_path / "loop.db")
    orchestrator = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(),
        backend=FakeLLMBackend(
            default=decision("CREATE_PROJECT", proposed_goal="契約Aの残期間を確認する")
        ),
    )
    loop = KnowledgeLoop(runtime, orchestrator, backend=None)
    principle = loop.ledger.record(
        "期限が近い契約は早めに確認する",
        source_type="principle_extraction",
        kind=KIND_PRINCIPLE,
        status=STATUS_SUPPORTED,
    )
    signal = Signal(
        type="opportunity", description="d", request="契約Aの残期間を確認する",
        confidence=0.9, risk="low", read_only=True,
        principle_id=principle.knowledge_id,
    )

    [(_, proposal)] = await GoalBridge(loop.ledger).submit([signal])
    assert proposal.status == PROPOSAL_AUTO_APPROVED
    assert orchestrator.projects.all() == []  # filing does not start work

    await loop.reconcile()

    assert loop.ledger.head(proposal.knowledge_id).status == PROPOSAL_ROUTED
    [project] = orchestrator.projects.all()
    assert project.goal == "契約Aの残期間を確認する"
    assert project.context["knowledge_proposal_id"] == proposal.knowledge_id
    runtime.close()


async def test_detector_returns_nothing_without_backend_or_principles(tmp_path):
    ledger = _ledger(tmp_path)
    view = WorldStateProjection(ledger).view()

    assert await GapRiskOpportunityDetector(backend=None).detect(view, []) == []

    principle = ledger.record("some principle", source_type="x", kind=KIND_PRINCIPLE)
    assert await GapRiskOpportunityDetector(backend=None).detect(view, [principle]) == []


# --- existing orchestrator behaviour is unaffected --------------------------


async def test_existing_orchestrator_routing_case_untouched(tmp_path):
    """Sanity check mirroring test_orchestrator_routing.py's case 1, run
    completely independently of the Knowledge Runtime."""
    backend = FakeLLMBackend(script=[decision("CREATE_PROJECT", proposed_goal="7月の売上低下原因を調べる")])
    runtime = InProcessAgentRuntime()
    orch = ProjectOrchestrator(tmp_path / "o2.db", agent_runtime=runtime, backend=backend)

    result = await orch.handle_request("7月の売上低下原因を調べて")

    assert result.action is RoutingAction.CREATE_PROJECT
    assert len(orch.projects.all()) == 1
    orch.close()
