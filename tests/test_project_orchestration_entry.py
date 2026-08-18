"""Phase A: an ordinary incoming message becomes Project work.

`nexus-seed task`, a webhook and any other connector all arrive the same way —
as a ``human_message`` through the existing Ingress — so this drives that
boundary rather than calling the orchestrator directly.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.ingress.models import IngressEnvelope, IngressStatus
from nexus_seed.orchestrator import (
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    completed_message,
)
from nexus_seed.processes.project_orchestration import (
    ROUTE_REQUEST,
    bootstrap_project_orchestration,
)
from nexus_seed.runtime.runtime import Runtime

REQUEST = "samples/sample_sales.csvを分析して2026年7月の売上低下原因を調べて"


def create_decision(goal=REQUEST):
    return proposal_response(
        {"action": "CREATE_PROJECT", "proposed_goal": goal, "reason": "new", "confidence": 0.9}
    )


def orchestrator(db, behaviour=None):
    return ProjectOrchestrator(
        db,
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=FakeLLMBackend(default=create_decision()),
    )


def message(text=REQUEST, *, key="msg-1") -> IngressEnvelope:
    """One human message as an adapter would hand it to the Ingress."""
    return IngressEnvelope(
        adapter_id="webhook",
        source_type="webhook",
        source_event_key=key,
        event_type="human_message",
        payload={"text": text},
    )


async def test_human_message_routes_to_project_orchestrator_when_enabled(tmp_path):
    runtime = Runtime(tmp_path / "app.db")
    done = lambda config, envelope: [completed_message(config.project_id, "分析完了")]
    orch = orchestrator(runtime.db, done)
    assert bootstrap_project_orchestration(runtime, orch, enabled=True) is True

    result = await runtime.ingress.ingest(message())

    assert result.status is IngressStatus.ACCEPTED
    [project] = orch.projects.all()
    assert project.goal == REQUEST
    assert project.status is ProjectStatus.COMPLETED
    assert project.summary == "分析完了"
    assert project.assigned_agent_id is not None
    runtime.close()


async def test_human_message_uses_legacy_runtime_when_disabled(tmp_path):
    runtime = Runtime(tmp_path / "app.db")
    orch = orchestrator(runtime.db)
    assert bootstrap_project_orchestration(runtime, orch, enabled=False) is False

    await runtime.ingress.ingest(message())

    # A strict no-op: no definition registered, no Project, nothing routed.
    assert runtime.process_store.get_definition(ROUTE_REQUEST.name, "1") is None
    assert orch.projects.all() == []
    assert runtime.project_orchestrator is None
    runtime.close()


async def test_duplicate_source_key_does_not_create_duplicate_project(tmp_path):
    runtime = Runtime(tmp_path / "app.db")
    orch = orchestrator(runtime.db)
    bootstrap_project_orchestration(runtime, orch, enabled=True)

    first = await runtime.ingress.ingest(message(key="delivery-7"))
    second = await runtime.ingress.ingest(message(key="delivery-7"))

    # Deduplication stays where it already was: one external delivery is one
    # Event, so the router is never asked about the same request twice.
    assert first.status is IngressStatus.ACCEPTED
    assert second.status is IngressStatus.DUPLICATE
    assert len(orch.projects.all()) == 1
    runtime.close()


async def test_a_message_with_no_text_is_not_a_failure(tmp_path):
    runtime = Runtime(tmp_path / "app.db")
    orch = orchestrator(runtime.db)
    bootstrap_project_orchestration(runtime, orch, enabled=True)

    await runtime.ingress.ingest(
        IngressEnvelope(
            adapter_id="webhook",
            source_type="webhook",
            source_event_key="empty-1",
            event_type="human_message",
            payload={"text": "   "},
        )
    )

    assert orch.projects.all() == []
    failed = [
        instance
        for instance in runtime.process_store.all_instances()
        if instance.definition_name == ROUTE_REQUEST.name
        and instance.status.value == "FAILED"
    ]
    assert failed == []
    runtime.close()
