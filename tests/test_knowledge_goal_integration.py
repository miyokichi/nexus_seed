"""Phase K5 — Goal integration: World View x Principles -> Signal -> Project.

Uses the real, unmodified ProjectOrchestrator (same as
tests/test_orchestrator_routing.py) to prove the bridge only calls its public
API and never reaches into orchestrator internals.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge import GapRiskOpportunityDetector, GoalBridge, KnowledgeLedger, Signal
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

    routing_backend = FakeLLMBackend(
        script=[decision("CREATE_PROJECT", proposed_goal="意思決定前に主要な不確実性を低減する")]
    )
    runtime = InProcessAgentRuntime()
    orchestrator = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=runtime, backend=routing_backend)

    bridge = GoalBridge(ledger, orchestrator)
    results = await bridge.submit(signals)

    assert len(results) == 1
    signal, routing_decision, project = results[0]
    assert routing_decision.action is RoutingAction.CREATE_PROJECT
    assert project is not None
    assert project.goal == "意思決定前に主要な不確実性を低減する"
    assert project.status is ProjectStatus.ACTIVE

    # The outcome is written back to the Ledger, linked to the principle.
    signal_records = ledger.by_kind("signal")
    assert len(signal_records) == 1
    assert signal_records[0].derived_from == [principle.knowledge_id]
    assert signal_records[0].metadata["project_id"] == project.id
    orchestrator.close()


async def test_low_confidence_signal_is_not_submitted(tmp_path):
    ledger = _ledger(tmp_path)
    routing_backend = FakeLLMBackend()
    runtime = InProcessAgentRuntime()
    orchestrator = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=runtime, backend=routing_backend)
    bridge = GoalBridge(ledger, orchestrator, min_confidence=0.5)

    weak_signal = Signal(type="risk", description="maybe nothing", request="調べて", confidence=0.1)
    results = await bridge.submit([weak_signal])

    assert results == []
    assert orchestrator.projects.all() == []
    assert routing_backend.calls == []
    orchestrator.close()


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
