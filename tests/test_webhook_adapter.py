"""AT5 + AT6 + AT7 (spec §55, §56, §57): pushed deliveries.

Covers both levels: the transport-free contract, and a real socket, so the HTTP
plumbing is exercised rather than assumed.
"""

from __future__ import annotations

import asyncio
import json

from ingress_helpers import ingress

from nexus_seed.adapters.webhook import (
    WebhookAdapter,
    WebhookIngress,
    WebhookServer,
    _adapter_id_from_path,
    _token_from,
)
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.runtime.runtime import Runtime

TOKEN = "test-shared-secret"


def hook(runtime, *, token=TOKEN) -> WebhookIngress:
    return WebhookIngress(ingress(runtime), token=token)


def delivery(key="delivery-1", *, event_type="human_message", **extra) -> dict:
    body = {
        "source_event_key": key,
        "event_type": event_type,
        "payload": {"text": "D1のCD解析が完了しました"},
    }
    body.update(extra)
    return body


# --- adapter ---------------------------------------------------------------


def test_the_adapter_takes_its_identity_from_the_provider():
    envelope = WebhookAdapter().envelope(delivery("abc-123"))
    assert envelope.source_event_key == "abc-123"
    assert envelope.source_type == "webhook"
    assert envelope.event_type == "human_message"


def test_a_body_without_a_key_gets_no_invented_one():
    """An invented key would make every redelivery look new."""
    envelope = WebhookAdapter().envelope({"event_type": "x", "payload": {}})
    assert envelope.source_event_key == ""


def test_optional_observed_at_is_parsed_or_defaulted():
    from datetime import datetime, timezone

    parsed = WebhookAdapter().envelope(delivery(observed_at="2026-08-13T09:30:00+00:00"))
    assert parsed.observed_at == datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)

    fallback = WebhookAdapter().envelope(delivery(observed_at="nonsense"))
    assert fallback.observed_at is not None


def test_path_and_token_parsing():
    assert _adapter_id_from_path("/ingress/github") == "github"
    assert _adapter_id_from_path("/ingress/github?x=1") == "github"
    assert _adapter_id_from_path("/ingress/") is None
    assert _adapter_id_from_path("/other/github") is None
    assert _token_from({"authorization": "Bearer abc"}) == "abc"
    assert _token_from({"x-ingress-token": "abc"}) == "abc"
    assert _token_from({}) is None


# --- ingress contract ------------------------------------------------------


async def test_a_delivery_is_accepted_and_becomes_an_event(tmp_path):
    """AT5."""
    runtime = Runtime(tmp_path / "wh.db")
    webhook = hook(runtime)

    response = await webhook.handle("webhook", delivery(), token=TOKEN)
    await webhook.drain_pending()

    assert response.status_code == 202
    assert response.body["status"] == "accepted"
    assert response.body["duplicate"] is False

    events = runtime.event_store.all()
    assert len(events) == 1
    assert events[0].source == "webhook"
    assert str(events[0].id) == response.body["event_id"]
    assert runtime.get_ingress_receipts()[0].source_event_key == "delivery-1"
    runtime.close()


async def test_a_duplicate_delivery_succeeds_without_a_second_event(tmp_path):
    """AT6: the provider did its job — say 200, not 500, and change nothing."""
    runtime = Runtime(tmp_path / "wh.db")
    webhook = hook(runtime)

    first = await webhook.handle("webhook", delivery("abc-123"), token=TOKEN)
    second = await webhook.handle("webhook", delivery("abc-123"), token=TOKEN)
    await webhook.drain_pending()

    assert first.status_code == 202 and first.body["duplicate"] is False
    assert second.status_code == 200 and second.body["duplicate"] is True
    assert second.body["event_id"] == first.body["event_id"]
    assert len(runtime.event_store.all()) == 1
    runtime.close()


async def test_an_invalid_body_is_refused(tmp_path):
    """AT7."""
    runtime = Runtime(tmp_path / "wh.db")
    webhook = hook(runtime)

    response = await webhook.handle(
        "webhook", {"event_type": "human_message", "payload": {}}, token=TOKEN
    )

    assert response.status_code == 400
    assert "empty source_event_key" in response.body["reasons"]
    assert runtime.event_store.all() == []
    runtime.close()


async def test_a_non_object_body_is_refused(tmp_path):
    runtime = Runtime(tmp_path / "wh.db")
    response = await hook(runtime).handle("webhook", ["not", "an", "object"], token=TOKEN)
    assert response.status_code == 400
    assert runtime.event_store.all() == []
    runtime.close()


async def test_different_adapter_ids_get_separate_key_spaces(tmp_path):
    runtime = Runtime(tmp_path / "wh.db")
    webhook = hook(runtime)

    a = await webhook.handle("github", delivery("1"), token=TOKEN)
    b = await webhook.handle("payroll", delivery("1"), token=TOKEN)
    await webhook.drain_pending()

    assert a.status_code == 202 and b.status_code == 202
    assert len(runtime.event_store.all()) == 2
    assert {r.adapter_id for r in runtime.get_ingress_receipts()} == {"github", "payroll"}
    runtime.close()


async def test_the_response_does_not_wait_for_the_work_it_causes(tmp_path):
    """Spec §29: the HTTP call returns once the Event is durable, not once it is handled."""
    runtime = Runtime(tmp_path / "wh.db")
    async def record(ctx):
        ctx.state.set("D1_CD", "target", ctx.event.payload["new"], source_event=ctx.event.id)
        return ctx.complete()

    runtime.register_process(
        ProcessDefinition(
            name="record_change",
            version="1",
            handler="record_change",
            trigger_event_types=("process_parameter_changed",),
        ),
        record,
    )
    webhook = hook(runtime)

    response = await webhook.handle(
        "webhook",
        delivery(
            "change-1",
            event_type="process_parameter_changed",
            payload={"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        ),
        token=TOKEN,
    )

    # Responded already; the pipeline has not run yet.
    assert response.status_code == 202
    assert runtime.state_store.get("D1_CD", "target") is None

    await webhook.drain_pending()
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()


# --- real socket -----------------------------------------------------------


async def post_json(port: int, path: str, body: dict, *, token: str | None = TOKEN):
    """POST ``body`` over a real TCP connection and return (status, parsed body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    raw = json.dumps(body).encode("utf-8")
    headers = [
        f"POST {path} HTTP/1.1",
        "Host: 127.0.0.1",
        "Content-Type: application/json",
        f"Content-Length: {len(raw)}",
    ]
    if token is not None:
        headers.append(f"Authorization: Bearer {token}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + raw)
    await writer.drain()

    response = await reader.read()
    writer.close()
    head, _, payload = response.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].split()[1])
    return status, json.loads(payload or b"{}")


async def test_a_real_http_post_ingests(tmp_path):
    runtime = Runtime(tmp_path / "http.db")
    webhook = hook(runtime)
    server = await WebhookServer(webhook).start()
    try:
        status, body = await post_json(
            server.bound_port, "/ingress/github", delivery("http-1")
        )
        assert status == 202
        assert body["duplicate"] is False

        status, body = await post_json(
            server.bound_port, "/ingress/github", delivery("http-1")
        )
        assert status == 200
        assert body["duplicate"] is True
    finally:
        await server.stop()

    assert len(runtime.event_store.all()) == 1
    assert runtime.get_ingress_receipts()[0].adapter_id == "github"
    runtime.close()


async def test_http_errors_for_bad_method_path_and_json(tmp_path):
    runtime = Runtime(tmp_path / "http.db")
    server = await WebhookServer(hook(runtime)).start()
    try:
        port = server.bound_port
        status, _ = await post_json(port, "/nope", delivery())
        assert status == 404

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /ingress/x HTTP/1.1\r\nHost: h\r\n\r\n")
        await writer.drain()
        head = (await reader.read()).split(b"\r\n")[0]
        writer.close()
        assert b"405" in head

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /ingress/x HTTP/1.1\r\nHost: h\r\nContent-Length: 3\r\n"
            b"Authorization: Bearer " + TOKEN.encode() + b"\r\n\r\n{{{"
        )
        await writer.drain()
        head = (await reader.read()).split(b"\r\n")[0]
        writer.close()
        assert b"400" in head
    finally:
        await server.stop()

    assert runtime.event_store.all() == []
    runtime.close()
