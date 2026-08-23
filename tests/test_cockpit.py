"""Project/Knowledge Cockpit projections, assets, and HTTP authentication."""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.cockpit import CockpitService, humanize_error
from nexus_seed.cockpit.assets import APP_JS, INDEX_HTML
from nexus_seed.core.event import Event
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
    response_headers = {
        name.lower(): value.strip()
        for line in head.decode("latin-1").split("\r\n")[1:]
        for name, _, value in [line.partition(":")]
    }
    return status, response_headers, body


async def test_snapshot_is_read_only_and_reports_current_application_sections(tmp_path):
    runtime = Runtime(tmp_path / "cockpit.db")
    try:
        await runtime.submit_event(Event("external_event", "test", {"text": "observed"}))
        before = runtime.db.conn.total_changes

        snapshot = CockpitService(runtime, master_id="operator").snapshot()

        assert runtime.db.conn.total_changes == before
        assert snapshot["overview"]["runtime"]["status"] == "ONLINE"
        assert set(snapshot) == {
            "generated_at",
            "overview",
            "needs_attention",
            "activities",
            "orchestrator",
            "knowledge",
            "system",
        }
        assert "phase6" not in snapshot["overview"]
        assert "work_counts" not in snapshot["system"]
        assert snapshot["activities"]
    finally:
        runtime.close()


async def test_cockpit_assets_and_snapshot_api_use_existing_auth(tmp_path):
    runtime = Runtime(tmp_path / "http.db")
    cockpit = CockpitService(runtime, master_id="operator")
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN), cockpit=cockpit
    ).start()
    try:
        status, headers, body = await get_path(server.bound_port, "/cockpit")
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        assert b"NEXUS SEED" in body and b"/cockpit/app.js" in body
        assert "script-src 'self'" in headers["content-security-policy"]
        assert "'unsafe-inline'" not in headers["content-security-policy"]

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


def test_navigation_exposes_only_current_product_surfaces():
    for view in ("overview", "knowledge", "activity", "orchestrator", "system"):
        assert f'data-view="{view}"' in INDEX_HTML
    for removed in ("being", "work", "reviews", "providers"):
        assert f'data-view="{removed}"' not in INDEX_HTML
    assert "const titles={overview:" in APP_JS


def test_human_error_translation_keeps_raw_fact_separate():
    raw = "schema validation failed: no proposed_state_deltas"
    translated = humanize_error(raw, source="interpret_event_llm")

    assert translated["raw_error"] == raw


def test_cockpit_refresh_is_manual_only():
    assert '$("#refresh").onclick=load' in APP_JS
    assert "setTimeout(load" not in APP_JS
    assert "state.timer" not in APP_JS
