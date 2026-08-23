"""Operational CLI helpers use ingress for writes and read-only SQLite for views."""

from __future__ import annotations

import asyncio
from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.operations import (
    read_status,
    submit_webhook_event,
)
from nexus_seed.runtime.runtime import Runtime


async def test_submit_webhook_event_uses_the_durable_ingress_boundary(tmp_path):
    runtime = Runtime(tmp_path / "runtime.db")
    ingress = WebhookIngress(runtime.ingress, token="secret")
    server = await WebhookServer(ingress, port=0).start()
    try:
        result = await asyncio.to_thread(
            submit_webhook_event,
            host="127.0.0.1",
            port=server.bound_port,
            token="secret",
            event_type="human_message",
            payload={"text": "inspect this"},
            source_event_key="test-task-1",
        )
        await ingress.drain_pending()

        assert result["status"] == "accepted"
        assert result["event_type"] == "human_message"
        events = runtime.event_store.by_type("human_message")
        assert len(events) == 1
        assert events[0].payload == {"text": "inspect this"}
    finally:
        await server.stop()
        runtime.close()


def test_status_reads_an_initialized_database(tmp_path):
    database = tmp_path / "runtime.db"
    runtime = Runtime(database)
    runtime.close()

    report = read_status(database)

    assert report["initialized"] is True
    assert report["counts"] == {
        "events": 0,
        "projects": 0,
        "agents": 0,
        "knowledge_revisions": 0,
        "pending_reviews": 0,
    }
