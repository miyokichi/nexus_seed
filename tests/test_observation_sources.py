"""Explicit observation sources stay scoped, durable and idempotent."""

from __future__ import annotations

import json
import pytest

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.cockpit import CockpitService
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.knowledge.autonomous_loop import KnowledgeLoop
from nexus_seed.knowledge.autonomous_loop import ENTITY_CONFIRMED
from nexus_seed.observation_sources import (
    ObservationSourceService,
    SYSTEM_SNAPSHOT_EVENT,
    SystemSnapshotAdapter,
)
from nexus_seed.orchestrator import (
    A2AMessage,
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
)
from nexus_seed.runtime.runtime import Runtime


def attach(runtime: Runtime, data_root) -> ObservationSourceService:
    service = ObservationSourceService(runtime, data_root=data_root)
    runtime.observation_sources = service
    return service


def test_source_requires_an_explicit_fixed_allowlist(tmp_path):
    runtime = Runtime(tmp_path / "source.db")
    service = attach(runtime, tmp_path)

    with pytest.raises(ValueError, match="at least one"):
        service.create_system_snapshot(name="PC", fields=[])
    with pytest.raises(ValueError, match="unsupported"):
        service.create_system_snapshot(name="PC", fields=["processes"])

    source = service.create_system_snapshot(name="PC", fields=["platform", "cpu"])
    values = SystemSnapshotAdapter(source, data_root=tmp_path).snapshot()
    assert set(values) == {"platform", "cpu"}
    assert "hostname" not in values
    runtime.close()


async def test_unchanged_snapshot_is_not_reingested_after_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "source.db"
    monkeypatch.setattr(
        SystemSnapshotAdapter,
        "snapshot",
        lambda self: {"cpu": {"logical_count": 8}},
    )
    runtime = Runtime(db_path)
    service = attach(runtime, tmp_path)
    source = service.create_system_snapshot(name="PC", fields=["cpu"])

    first = await service.poll_due(force_source_id=source.id)
    second = await service.poll_due(force_source_id=source.id)
    assert first[0]["status"] == "ACCEPTED"
    assert second == [{"source_id": source.id, "status": "UNCHANGED"}]
    assert len(runtime.event_store.by_type(SYSTEM_SNAPSHOT_EVENT)) == 1
    runtime.close()

    runtime2 = Runtime(db_path)
    service2 = attach(runtime2, tmp_path)
    third = await service2.poll_due(force_source_id=source.id)
    assert third == [{"source_id": source.id, "status": "UNCHANGED"}]
    assert len(runtime2.event_store.by_type(SYSTEM_SNAPSHOT_EVENT)) == 1
    restored = service2.store.get(source.id)
    assert restored is not None and restored.last_event_id is not None
    runtime2.close()


async def test_changed_snapshot_becomes_knowledge_and_world_state(tmp_path, monkeypatch):
    snapshots = iter(
        [
            {"memory": {"total_bytes": 100}},
            {"memory": {"total_bytes": 200}},
        ]
    )
    monkeypatch.setattr(SystemSnapshotAdapter, "snapshot", lambda self: next(snapshots))
    runtime = Runtime(tmp_path / "source.db")
    service = attach(runtime, tmp_path)
    source = service.create_system_snapshot(name="PC", fields=["memory"])
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator)
    runtime.knowledge_loop = loop

    await service.poll_due(force_source_id=source.id)
    first = await loop.reconcile()
    await service.poll_due(force_source_id=source.id)
    second = await loop.reconcile()

    assert first.source_observations == 1
    assert second.source_observations == 1
    assert loop.world_view()[f"system:{source.id}"]["memory.total_bytes"] == 200
    events = runtime.event_store.by_type(SYSTEM_SNAPSHOT_EVENT)
    assert len(events) == 2
    assert all(event.ingress_receipt_id is not None for event in events)
    runtime.close()


async def test_cockpit_can_pause_resume_and_poll_source(tmp_path, monkeypatch):
    monkeypatch.setattr(SystemSnapshotAdapter, "snapshot", lambda self: {"cpu": {"logical_count": 4}})
    runtime = Runtime(tmp_path / "source.db")
    attach(runtime, tmp_path)
    cockpit = CockpitService(runtime, master_id="operator")

    created = await cockpit.create_observation_source(
        name="Developer PC", fields=["cpu"], poll_interval_seconds=30
    )
    source_id = created["source"]["id"]
    assert created["outcomes"][0]["status"] == "ACCEPTED"
    assert cockpit.set_observation_source_enabled(source_id, False)["enabled"] is False
    assert await cockpit.poll_observation_source(source_id) == {
        "source": cockpit.runtime.observation_sources.store.get(source_id).to_dict(),
        "outcomes": [],
    }
    assert cockpit.set_observation_source_enabled(source_id, True)["enabled"] is True
    assert (await cockpit.poll_observation_source(source_id))["outcomes"][0]["status"] == "UNCHANGED"
    runtime.close()


async def test_confirming_entity_reprojects_observation_to_canonical_id(tmp_path):
    runtime = Runtime(tmp_path / "entity.db")
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator)
    message = A2AMessage(
        id="entity-report-1",
        type=A2AMessageType.PROJECT_STATUS,
        project_id="project-1",
        source_agent_id="agent-1",
        payload={
            "summary": "名称が曖昧です",
            "observations": [
                {
                    "entity": "suggested-customer",
                    "entity_label": "ACME",
                    "attribute": "status",
                    "value": "active",
                    "confidence": 0.6,
                }
            ],
        },
    )
    orchestrator.gateway.record([message])
    await loop.reconcile()
    [candidate] = loop.entity_candidates()

    decided = loop.decide_entity(
        candidate.knowledge_id,
        "confirm",
        canonical_id="customer:acme",
        actor="operator",
    )
    history_size = len(loop.ledger.history(candidate.knowledge_id))
    again = loop.decide_entity(
        candidate.knowledge_id,
        "confirm",
        canonical_id="customer:acme",
        actor="operator",
    )

    assert decided.status == ENTITY_CONFIRMED
    assert again.id == decided.id
    assert len(loop.ledger.history(candidate.knowledge_id)) == history_size
    assert loop.world_view()["customer:acme"]["status"] == "active"
    observation = loop.ledger.head(candidate.metadata["observation_id"])
    assert any(
        relation.type == "about" and relation.target == "customer:acme"
        for relation in observation.relations
    )
    runtime.close()


async def test_observation_source_cockpit_endpoint_and_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(SystemSnapshotAdapter, "snapshot", lambda self: {"cpu": {"logical_count": 2}})
    runtime = Runtime(tmp_path / "http-source.db")
    attach(runtime, tmp_path)
    cockpit = CockpitService(runtime, master_id="operator")
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token="source-token"), cockpit=cockpit
    )

    unauthorized = await server._knowledge_action_response(
        "/cockpit/api/knowledge/sources", {}, b"{}"
    )
    created = await server._knowledge_action_response(
        "/cockpit/api/knowledge/sources",
        {"authorization": "Bearer source-token"},
        json.dumps(
            {"name": "PC", "fields": ["cpu"], "poll_interval_seconds": 10}
        ).encode(),
    )
    source_id = created.body["source"]["id"]
    disabled = await server._knowledge_action_response(
        f"/cockpit/api/knowledge/sources/{source_id}/disable",
        {"authorization": "Bearer source-token"},
        b"{}",
    )

    assert unauthorized.status_code == 401
    assert created.status_code == 200
    assert disabled.body["enabled"] is False
    assert "observation-source-form" in APP_JS
    assert "/cockpit/api/knowledge/sources" in APP_JS
    runtime.close()


async def test_one_broken_source_does_not_stop_another(tmp_path, monkeypatch):
    def snapshot(adapter):
        if adapter.source.name == "broken":
            raise OSError("sensor unavailable")
        return {"cpu": {"logical_count": 6}}

    monkeypatch.setattr(SystemSnapshotAdapter, "snapshot", snapshot)
    runtime = Runtime(tmp_path / "errors.db")
    service = attach(runtime, tmp_path)
    broken = service.create_system_snapshot(name="broken", fields=["cpu"])
    healthy = service.create_system_snapshot(name="healthy", fields=["cpu"])

    outcomes = await service.poll_due()

    assert outcomes[0] == {
        "source_id": broken.id,
        "status": "ERROR",
        "error": "sensor unavailable",
    }
    assert outcomes[1]["source_id"] == healthy.id
    assert outcomes[1]["status"] == "ACCEPTED"
    assert service.store.get(broken.id).last_error == "sensor unavailable"
    runtime.close()


async def test_entity_candidate_is_recovered_after_partial_reconcile(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "entity-recovery.db")
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator)
    message = A2AMessage(
        id="partial-entity-report",
        type=A2AMessageType.PROJECT_STATUS,
        project_id="project-1",
        source_agent_id="agent-1",
        payload={
            "summary": "entity report",
            "observations": [
                {
                    "entity": "suggested",
                    "entity_label": "Unknown Corp",
                    "attribute": "status",
                    "value": "active",
                    "confidence": 0.5,
                }
            ],
        },
    )
    orchestrator.gateway.record([message])
    original = KnowledgeLoop._record_entity_candidate
    monkeypatch.setattr(KnowledgeLoop, "_record_entity_candidate", lambda *args: None)
    await loop.reconcile()
    assert loop.entity_candidates() == []

    monkeypatch.setattr(KnowledgeLoop, "_record_entity_candidate", original)
    await loop.reconcile()
    assert len(loop.entity_candidates()) == 1
    runtime.close()
