"""Operational CLI helpers use ingress for writes and read-only SQLite for views."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.core.continuation import Continuation
from nexus_seed.core.process import ProcessInstance, ProcessStatus
from nexus_seed.operations import (
    build_review_payload,
    read_pending_reviews,
    read_status,
    resolve_pending_review,
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
        "continuations": 0,
        "pending_reviews": 0,
    }


def test_pending_review_is_discovered_and_payload_keeps_match_id(tmp_path):
    database = tmp_path / "runtime.db"
    runtime = Runtime(database)
    proposal_id = str(uuid.uuid4())
    instance = ProcessInstance(
        "interpret_event_llm",
        "1",
        status=ProcessStatus.SUSPENDED,
    )
    runtime.process_store.save_instance(instance)
    runtime.continuation_store.save(
        Continuation(
            process_instance_id=instance.id,
            resume_point="await_review",
            waiting_for={
                "event_type": "interpretation_reviewed",
                "proposal_id": proposal_id,
            },
            saved_process_state={"proposal_id": proposal_id},
        )
    )
    runtime.close()

    reviews = read_pending_reviews(database)
    resolved = resolve_pending_review(database, proposal_id)
    payload = build_review_payload(resolved, "APPROVE")

    assert len(reviews) == 1
    assert resolved.event_type == "interpretation_reviewed"
    assert payload == {"proposal_id": proposal_id, "decision": "approve"}


def test_review_payload_refuses_to_replace_the_continuation_match(tmp_path):
    database = tmp_path / "runtime.db"
    runtime = Runtime(database)
    instance = ProcessInstance("action_validator", "1", status=ProcessStatus.SUSPENDED)
    runtime.process_store.save_instance(instance)
    runtime.continuation_store.save(
        Continuation(
            process_instance_id=instance.id,
            resume_point="await_action_review",
            waiting_for={"event_type": "action_reviewed", "proposal_id": "original"},
        )
    )
    runtime.close()

    review = read_pending_reviews(database)[0]
    with pytest.raises(RuntimeError, match="cannot replace"):
        build_review_payload(review, "approve", {"proposal_id": "different"})
