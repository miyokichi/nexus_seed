"""Human Cockpit: read-only projections, causal activity, auth and Control Plane."""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.cockpit import CockpitService, humanize_error
from nexus_seed.control.models import HumanIdentity
from nexus_seed.core.event import Event
from nexus_seed.processes.control import bootstrap_control
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


TOKEN = "cockpit-test-token"


async def get_path(port: int, path: str, *, token: str | None = None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    headers = [f"GET {path} HTTP/1.1", "Host: 127.0.0.1"]
    if token is not None:
        headers.append(f"Authorization: Bearer {token}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, body = response.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    header_lines = head.decode("latin-1").split("\r\n")[1:]
    response_headers = {
        name.lower(): value.strip()
        for line in header_lines
        for name, _, value in [line.partition(":")]
    }
    return status, response_headers, body


async def post_control(port: int, command: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(
        {
            "command": command,
            "source_channel": "cockpit-test",
            "source_message_id": "message-1",
            "idempotency_key": "cockpit-test:message-1",
        }
    ).encode("utf-8")
    request = (
        "POST /control HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {TOKEN}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("latin-1") + body
    writer.write(request)
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, payload = response.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(payload)


async def test_snapshot_is_read_only_and_groups_causal_activity(tmp_path):
    runtime = Runtime(tmp_path / "cockpit.db")
    bootstrap_semantic(runtime)
    bootstrap_control(runtime)
    bootstrap_persistent_being(runtime, enabled=True, wake_on_start=False)
    try:
        await runtime.submit_event(
            Event("external_event", "test", {"importance": "high", "subject": "D1_CD"})
        )
        before = runtime.db.conn.total_changes

        snapshot = CockpitService(
            runtime, phase6_enabled=True, master_id="operator"
        ).snapshot()

        assert runtime.db.conn.total_changes == before
        assert snapshot["overview"]["runtime"]["status"] == "ONLINE"
        assert snapshot["overview"]["phase6"]["enabled"] is True
        activity = next(
            item
            for item in snapshot["activities"]
            if item["source_event"] and item["source_event"]["type"] == "external_event"
        )
        assert "重要性と現在の関心との関連を評価" in activity["steps"]
        assert any(
            detail["definition"] == "attention_evaluation@1"
            for detail in activity["details"]["processes"]
        )
        assert not any(
            item["title"] == "attention_evaluation" for item in snapshot["activities"]
        )
    finally:
        runtime.close()


async def test_cockpit_assets_and_snapshot_api_use_existing_auth(tmp_path):
    runtime = Runtime(tmp_path / "http.db")
    cockpit = CockpitService(runtime, phase6_enabled=False, master_id="operator")
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN), cockpit=cockpit
    ).start()
    try:
        status, headers, body = await get_path(server.bound_port, "/cockpit")
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        assert b"NEXUS SEED" in body and b"/cockpit/app.js" in body

        status, _, body = await get_path(server.bound_port, "/cockpit/api/snapshot")
        assert status == 401
        assert json.loads(body)["error"] == "unauthorized"

        status, headers, body = await get_path(
            server.bound_port, "/cockpit/api/snapshot", token=TOKEN
        )
        assert status == 200
        assert headers["content-type"].startswith("application/json")
        assert json.loads(body)["overview"]["runtime"]["status"] == "ONLINE"
    finally:
        await server.stop()
        runtime.close()


async def test_disabled_cockpit_does_not_change_webhook_server(tmp_path):
    runtime = Runtime(tmp_path / "disabled.db")
    server = await WebhookServer(WebhookIngress(runtime.ingress, token=TOKEN)).start()
    try:
        status, _, body = await get_path(server.bound_port, "/cockpit")
        assert status == 404
        assert json.loads(body)["error"] == "cockpit is disabled"
    finally:
        await server.stop()
        runtime.close()


async def test_self_question_answer_uses_control_plane_and_phase6_process(tmp_path):
    runtime = Runtime(tmp_path / "answer.db")
    bootstrap_semantic(runtime)
    bootstrap_control(runtime)
    bootstrap_persistent_being(runtime, enabled=True, wake_on_start=False)
    runtime.control_store.save_identity(
        HumanIdentity(
            identity_id="operator",
            display_name="Operator",
            permissions=("command.*",),
        )
    )
    runtime.state_store.set(
        "self", "unresolved_questions", [{"question": "Which target is preferred?"}]
    )
    snapshot = CockpitService(
        runtime, phase6_enabled=True, master_id="operator"
    ).snapshot()
    question_id = snapshot["being"]["self"]["unresolved_questions"][0]["id"]
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        console=runtime.console,
        control_identity_id="operator",
        cockpit=CockpitService(runtime, phase6_enabled=True, master_id="operator"),
    ).start()
    try:
        status, result = await post_control(
            server.bound_port,
            f'/answer {question_id} answer="Use target A"',
        )
        assert status == 200
        assert result["status"] == "EXECUTED"

        await runtime.run_pending()

        assert runtime.state_store.get("self", "unresolved_questions") == []
        beliefs = runtime.state_store.get("self", "beliefs")
        assert beliefs[-1]["answer"] == "Use target A"
        assert beliefs[-1]["claim_status"] == "CONFIRMED"
        assert runtime.control_store.command_by_idempotency_key(
            "cockpit-test:message-1"
        ).command_type == "self.question.answer"
    finally:
        await server.stop()
        runtime.close()


def test_human_error_translation_keeps_raw_fact_separate():
    raw = "schema validation failed: no proposed_state_deltas"
    translated = humanize_error(raw, source="interpret_event_llm")

    assert translated["title"] == "LLMの解釈結果を採用できませんでした"
    assert "World State更新は行われていません" in translated["message"]
    assert translated["raw_error"] == raw
