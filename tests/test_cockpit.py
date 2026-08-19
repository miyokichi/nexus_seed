"""Human Cockpit: read-only projections, causal activity, auth and Control Plane."""

from __future__ import annotations

import asyncio
import json

from extension_helpers import POWERPOINT, block, extension_runtime, gap_work, needs

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.autonomy.models import (
    AcquisitionAttempt,
    AcquisitionStage,
    AcquisitionStatus,
    AcquisitionSubscriber,
    AutonomyDecision,
    AutonomyDecisionKind,
    CapabilityAcquisitionSession,
)
from nexus_seed.capabilities.models import CapabilityRequirement
from nexus_seed.cockpit import CockpitService, humanize_error
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.core.event import Event
from nexus_seed.extension.models import CapabilityGap
from nexus_seed.presence.models import IntentionRecord
from nexus_seed.orchestrator.models import Project, ProjectStatus
from nexus_seed.processes.autonomy import bootstrap_autonomy
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.orchestrator.pursuits import ProjectPursuits
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.storage.orchestrator_store import ProjectStore
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


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


async def test_snapshot_is_read_only_and_groups_causal_activity(tmp_path):
    runtime = Runtime(tmp_path / "cockpit.db")
    bootstrap_semantic(runtime)
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
        assert "script-src 'self'" in headers["content-security-policy"]
        assert "'unsafe-inline'" not in headers["content-security-policy"]

        status, headers, body = await get_path(
            server.bound_port, "/cockpit/styles.css"
        )
        assert status == 200
        assert headers["content-type"].startswith("text/css")
        assert b".shell{display:grid" in body
        assert b".sidebar{position:sticky" in body
        assert b".assistance-card{" in body

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


def test_human_error_translation_keeps_raw_fact_separate():
    raw = "schema validation failed: no proposed_state_deltas"
    translated = humanize_error(raw, source="interpret_event_llm")

    assert translated["title"] == "LLMの解釈結果を採用できませんでした"
    assert "World State更新は行われていません" in translated["message"]
    assert translated["raw_error"] == raw


def test_cockpit_refresh_is_manual_only():
    assert '$("#refresh").onclick=load' in APP_JS
    assert "setTimeout(load" not in APP_JS
    assert "state.timer" not in APP_JS


def test_capability_assistance_aggregates_goal_trace_without_writing(tmp_path):
    runtime = Runtime(tmp_path / "assistance.db")
    projects = ProjectStore(runtime.db)
    project = Project(
        goal="LLMサーバーの状態を診断する",
        summary="Self operation",
        status=ProjectStatus.ACTIVE,
    )
    projects.save(project)
    runtime.register_pursuit_source("orchestrator.projects", ProjectPursuits(projects))
    intention = IntentionRecord.for_pursuit(
        project.id,
        "診断結果を得て次の対応を決める",
        reason="Projectを具体的なWorkへ分解した",
    )
    runtime.state_store.set(f"intention:{intention.id}", "record", intention.to_dict())

    works = []
    gaps = []
    requirement = CapabilityRequirement(name="diagnostic_probe")
    for index in range(2):
        work = WorkRequirement(
            work_type="diagnose_llm",
            work_key=f"diagnose:{index}",
            objective=f"診断対象 {index + 1} を確認する",
            reason="Goal達成に診断が必要",
            goal_id=project.id,
            required_capabilities=[requirement],
            missing_capabilities=["diagnostic_probe"],
            status=WorkStatus.BLOCKED_CAPABILITY,
        )
        runtime.work_requirement_store.save(work)
        gap = CapabilityGap(
            work_requirement_id=work.id,
            required_capabilities=[requirement],
            missing_capabilities=[requirement],
            reason="no capable process",
        )
        runtime.extension_store.save_gap(gap)
        works.append(work)
        gaps.append(gap)

    session = CapabilityAcquisitionSession(
        capability_gap_id=gaps[0].id,
        source_work_requirement_id=works[0].id,
        acquisition_key="diagnostic-probe",
        target_capabilities=[{"name": "diagnostic_probe"}],
        status=AcquisitionStatus.BLOCKED,
        current_stage=AcquisitionStage.EXTENSION,
        blocked_reason="AUTONOMY_FORBIDDEN: no authorized provider",
    )
    runtime.autonomy_store.save_session(session)
    for work in works:
        runtime.autonomy_store.subscribe(
            AcquisitionSubscriber(
                acquisition_session_id=session.id,
                work_requirement_id=work.id,
            )
        )
    runtime.autonomy_store.save_decision(
        AutonomyDecision(
            acquisition_session_id=session.id,
            stage=AcquisitionStage.EXTENSION,
            decision=AutonomyDecisionKind.FORBIDDEN,
            evaluated_risk="CRITICAL",
            reasons=["provider permission is forbidden"],
        )
    )
    runtime.autonomy_store.save_attempt(
        AcquisitionAttempt(
            acquisition_session_id=session.id,
            attempt_type="provider_discovery",
            attempt_number=1,
            status="FAILED",
            failure_reason="no authorized provider",
        )
    )
    try:
        before = runtime.db.conn.total_changes
        snapshot = CockpitService(
            runtime, phase6_enabled=True, master_id="operator"
        ).snapshot()

        assert runtime.db.conn.total_changes == before
        assert len(snapshot["capability_assistance"]) == 1
        assistance = snapshot["capability_assistance"][0]
        assert assistance["goal"]["title"] == "Self operation"
        assert assistance["intention"]["focus"] == "診断結果を得て次の対応を決める"
        assert assistance["purpose"] == "診断結果を得て次の対応を決める"
        assert assistance["work_count"] == 2
        assert assistance["missing_capabilities"] == ["diagnostic_probe"]
        assert assistance["human_action"]["type"] == "FORBIDDEN"
        assert any("policy=FORBIDDEN" in line for line in assistance["automatic_acquisition"]["tried"])
        assert any("provider_discovery" in line for line in assistance["automatic_acquisition"]["tried"])
        assert [item["kind"] for item in snapshot["needs_attention"]].count(
            "capability_assistance"
        ) == 1
        assert not any(
            item["kind"] == "blocked_work" for item in snapshot["needs_attention"]
        )
    finally:
        runtime.close()


def test_capability_assistance_waits_while_automatic_acquisition_is_active(tmp_path):
    runtime = Runtime(tmp_path / "active-acquisition.db")
    requirement = CapabilityRequirement(name="diagnostic_probe")
    work = WorkRequirement(
        work_type="diagnose_llm",
        work_key="diagnose:active",
        objective="LLMを診断する",
        required_capabilities=[requirement],
        missing_capabilities=["diagnostic_probe"],
        status=WorkStatus.BLOCKED_CAPABILITY,
    )
    runtime.work_requirement_store.save(work)
    gap = CapabilityGap(
        work_requirement_id=work.id,
        required_capabilities=[requirement],
        missing_capabilities=[requirement],
    )
    runtime.extension_store.save_gap(gap)
    session = CapabilityAcquisitionSession(
        capability_gap_id=gap.id,
        source_work_requirement_id=work.id,
        acquisition_key="diagnostic-active",
        target_capabilities=[{"name": "diagnostic_probe"}],
        status=AcquisitionStatus.ANALYZING,
        current_stage=AcquisitionStage.EXTENSION,
    )
    runtime.autonomy_store.save_session(session)
    runtime.autonomy_store.subscribe(
        AcquisitionSubscriber(
            acquisition_session_id=session.id,
            work_requirement_id=work.id,
        )
    )
    try:
        snapshot = CockpitService(
            runtime, phase6_enabled=False, master_id="operator"
        ).snapshot()
        assert snapshot["capability_assistance"] == []
        assert not any(
            item["kind"] in {"capability_assistance", "blocked_work"}
            for item in snapshot["needs_attention"]
        )
    finally:
        runtime.close()


async def test_capability_assistance_links_existing_acquisition_review(tmp_path):
    runtime = extension_runtime(tmp_path, "assistance-review.db")
    bootstrap_autonomy(runtime)
    work = gap_work(
        runtime,
        required=[needs("parse_powerpoint", POWERPOINT)],
    )
    try:
        await block(runtime, work)
        snapshot = CockpitService(
            runtime, phase6_enabled=False, master_id="operator"
        ).snapshot()

        assert len(snapshot["capability_assistance"]) == 1
        assistance = snapshot["capability_assistance"][0]
        assert assistance["human_action"]["type"] == "REVIEW"
        assert assistance["review_ids"]
        assert assistance["reviews"][0]["category"] == "capability_acquisition"
        assert not any(
            item["kind"] == "capability_acquisition"
            and item["target_id"] in assistance["review_ids"]
            for item in snapshot["needs_attention"]
        )
    finally:
        runtime.close()


def test_capability_assistance_ui_uses_existing_control_boundaries():
    assert "function capabilityAssistanceCard" in APP_JS
    assert "自動解決で試したこと" in APP_JS
    assert "必要な対応" in APP_JS
    assert 'data-action="${esc(action)}"' in APP_JS
    assert 'app.addEventListener("click"' in APP_JS
    assert 'action==="review-approve"' in APP_JS
    assert " onclick=" not in APP_JS


