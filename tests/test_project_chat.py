"""Project Chat explains one project read-only and never changes its state."""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends.llm import FakeLLMBackend, invalid_response, proposal_response
from nexus_seed.chat.guards import detect_state_change_request
from nexus_seed.chat.models import ChatAnswerStatus, ChatRole
from nexus_seed.chat.service import ProjectChatService
from nexus_seed.cockpit import CockpitService
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.control.models import Goal
from nexus_seed.core.continuation import Continuation
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessInstance, ProcessStatus
from nexus_seed.presence.models import IntentionRecord
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


TOKEN = "project-chat-token"


def _answer(text: str, *, certainty: str = "FACT", references=()) -> dict:
    return {"answer": text, "certainty": certainty, "references": list(references)}


def _backend(*answers: dict) -> FakeLLMBackend:
    return FakeLLMBackend([proposal_response(item) for item in answers])


def _goal(runtime: Runtime, project_id: str, *, title: str) -> Goal:
    goal = Goal(
        title=title,
        objective=f"{title} を完了させる",
        owner_identity_id="operator",
        metadata={"project_id": project_id, "project_title": title},
    )
    runtime.control_store.save_goal(goal)
    return goal


def _project_runtime(tmp_path, *, backend=None, name: str = "chat.db") -> Runtime:
    """A runtime holding one blocked project and one unrelated project."""

    runtime = Runtime(tmp_path / name)
    if backend is not None:
        runtime.register_backend("llm", backend)
    goal = _goal(runtime, "project-a", title="Project A")
    intention = IntentionRecord.for_goal(goal.id, "測定の不確かさを解消する")
    runtime.state_store.set(
        f"intention:{intention.id}", "record", intention.to_dict()
    )
    source = runtime.event_store.append(
        Event("deadline_updated", "test", {"project_id": "project-a"})
    )
    runtime.work_requirement_store.save(
        WorkRequirement(
            work_type="analyze_measurement",
            work_key="project-a:active",
            objective="最新の測定結果を解析する",
            goal_id=goal.id,
            source_event_id=source.id,
        )
    )
    runtime.work_requirement_store.save(
        WorkRequirement(
            work_type="render_report",
            work_key="project-a:blocked",
            objective="レポートを作成する",
            goal_id=goal.id,
            status=WorkStatus.BLOCKED_CAPABILITY,
            missing_capabilities=("document.render",),
        )
    )
    _goal(runtime, "project-b", title="Project B")
    return runtime


async def _request(port: int, method: str, path: str, *, token=TOKEN, body=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else b""
    if payload:
        lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(payload)}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, raw = response.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(raw)


async def test_questions_are_answered_from_the_project_situation(tmp_path):
    backend = _backend(
        _answer("残タスクは解析1件、レポート作成が停止中です。"),
        _answer("レポート作成が能力不足で停止しています。"),
        _answer("締め切り変更が直近の変化です。"),
        _answer("Project Aをレビュー可能にすることが現在のGoalです。"),
    )
    runtime = _project_runtime(tmp_path, backend=backend)
    chat = ProjectChatService(runtime)
    try:
        remaining = await chat.ask("project-a", "残りタスクは？")
        blocked = await chat.ask("project-a", "何で止まってる？")
        changed = await chat.ask("project-a", "最近何が変わった？")
        goal = await chat.ask("project-a", "今のGoalは？")

        for reply in (remaining, blocked, changed, goal):
            assert reply["status"] == ChatAnswerStatus.ANSWERED.value
            assert reply["project_id"] == "project-a"
            assert reply["thread_id"] == remaining["thread_id"]

        situation = backend.calls[0].context["project_situation"]
        assert situation["project_id"] == "project-a"
        assert [item["objective"] for item in situation["active_work"]] == [
            "最新の測定結果を解析する"
        ]
        assert situation["blocked_work"][0]["missing_capabilities"] == ["document.render"]
        assert any(
            item["type"] == "BLOCKED_CAPABILITY" for item in situation["blockers"]
        )
        assert situation["recent_changes"]
        assert [item["title"] for item in situation["active_goals"]] == ["Project A"]

        # The whole context is this project, its own thread, and the question.
        assert set(backend.calls[0].context) == {
            "project_situation",
            "recent_chat",
            "question",
        }
        assert "project-b" not in json.dumps(backend.calls[-1].context, default=str)
        assert [item["text"] for item in backend.calls[-1].context["recent_chat"]][
            0
        ] == "残りタスクは？"
    finally:
        runtime.close()


async def test_answering_changes_no_runtime_state(tmp_path):
    runtime = _project_runtime(tmp_path, backend=_backend(_answer("停止していません。")))
    chat = ProjectChatService(runtime)
    try:
        goals = runtime.control_store.goals()
        works = runtime.work_requirement_store.all()
        events = len(runtime.event_store.all())
        state = {
            (entry.entity, entry.attribute): entry.value
            for entry in runtime.state_store.all_current()
        }

        await chat.ask("project-a", "今どうなってる？")

        assert len(runtime.event_store.all()) == events
        assert runtime.control_store.goals() == goals
        assert runtime.work_requirement_store.all() == works
        assert {
            (entry.entity, entry.attribute): entry.value
            for entry in runtime.state_store.all_current()
        } == state
        assert runtime.process_store.all_instances() == []
    finally:
        runtime.close()


async def test_another_project_is_refused_as_out_of_scope(tmp_path):
    backend = _backend(_answer("答えてはいけません。"))
    runtime = _project_runtime(tmp_path, backend=backend)
    chat = ProjectChatService(runtime)
    try:
        reply = await chat.ask("project-a", "project-b の残タスクは？")

        assert reply["status"] == ChatAnswerStatus.OUT_OF_SCOPE.value
        assert "project-b" in reply["answer"]
        assert backend.calls == []

        allowed = await chat.ask("project-a", "このプロジェクトの状況は？")
        assert allowed["status"] == ChatAnswerStatus.ANSWERED.value
    finally:
        runtime.close()


async def test_state_change_requests_are_refused_without_touching_state(tmp_path):
    backend = _backend(_answer("実行してはいけません。"))
    runtime = _project_runtime(tmp_path, backend=backend)
    chat = ProjectChatService(runtime)
    try:
        work = runtime.work_requirement_store.get_by_work_key("project-a:active")
        for request in (
            f"Work {work.id} をキャンセルして",
            "レポート作成を優先して",
            "この方針で進めて",
            "please cancel the blocked work",
        ):
            reply = await chat.ask("project-a", request)
            assert reply["status"] == ChatAnswerStatus.READ_ONLY_REFUSED.value
            assert "read-only" in reply["answer"]

        assert backend.calls == []
        assert (
            runtime.work_requirement_store.get_by_work_key("project-a:active").status
            is WorkStatus.EXPECTED
        )
        assert runtime.event_store.by_type("work_cancelled") == []
    finally:
        runtime.close()


def test_questions_about_stopped_work_are_not_read_as_change_requests():
    for question in (
        "今どうなってる？",
        "何で止まってる？",
        "残りタスクは？",
        "最近何が変わった？",
        "次に何を確認すべき？",
        "which work is blocked?",
        "what changed recently?",
    ):
        assert detect_state_change_request(question) is None
    assert detect_state_change_request("Work Xをキャンセルして") == "キャンセルして"
    assert detect_state_change_request("Bを優先してください") == "優先してください"


async def test_malformed_llm_output_fails_safely(tmp_path):
    runtime = _project_runtime(
        tmp_path, backend=FakeLLMBackend([invalid_response()])
    )
    chat = ProjectChatService(runtime)
    try:
        events = len(runtime.event_store.all())
        reply = await chat.ask("project-a", "今どうなってる？")

        assert reply["status"] == ChatAnswerStatus.LLM_INVALID.value
        assert "レポートを作成する" in reply["answer"]
        assert len(runtime.event_store.all()) == events
        assert runtime.state_store.get_current("project:project-a", "answer") is None

        history = chat.history("project-a")["messages"]
        assert [item["role"] for item in history] == [
            ChatRole.HUMAN.value,
            ChatRole.NEXUS_SEED.value,
        ]
        assert history[-1]["metadata"]["reason"].startswith("LLM answer did not match")
    finally:
        runtime.close()


async def test_chat_answers_deterministically_when_no_llm_is_configured(tmp_path):
    runtime = _project_runtime(tmp_path)
    try:
        reply = await ProjectChatService(runtime).ask("project-a", "今どうなってる？")

        assert reply["status"] == ChatAnswerStatus.LLM_UNAVAILABLE.value
        assert "レポートを作成する" in reply["answer"]
        assert "人間のReview待ちはありません。" in reply["answer"]
    finally:
        runtime.close()


async def test_thread_survives_restart_and_uses_the_current_situation(tmp_path):
    database = tmp_path / "restart.db"
    runtime = _project_runtime(
        tmp_path, backend=_backend(_answer("停止中のWorkがあります。")), name="restart.db"
    )
    chat = ProjectChatService(runtime)
    try:
        first = await chat.ask("project-a", "何で止まってる？")
        thread_id = first["thread_id"]
    finally:
        runtime.close()

    backend = _backend(_answer("停止は解消しました。"))
    restarted = Runtime(database)
    restarted.register_backend("llm", backend)
    try:
        history = ProjectChatService(restarted).history("project-a")
        assert history["thread_id"] == thread_id
        assert [item["text"] for item in history["messages"]] == [
            "何で止まってる？",
            "停止中のWorkがあります。",
        ]

        blocked = restarted.work_requirement_store.get_by_work_key("project-a:blocked")
        restarted.work_requirement_store.update_status(blocked.id, WorkStatus.SATISFIED)

        reply = await ProjectChatService(restarted).ask("project-a", "今どうなってる？")
        assert reply["thread_id"] == thread_id
        situation = backend.calls[-1].context["project_situation"]
        assert situation["blocked_work"] == []
        assert [item["objective"] for item in situation["recently_completed_work"]] == [
            "レポートを作成する"
        ]
        assert [item["text"] for item in backend.calls[-1].context["recent_chat"]] == [
            "何で止まってる？",
            "停止中のWorkがあります。",
        ]
    finally:
        restarted.close()


async def test_chat_history_is_not_treated_as_confirmed_world_state(tmp_path):
    runtime = _project_runtime(tmp_path, backend=_backend(_answer("順調です。")))
    try:
        await ProjectChatService(runtime).ask("project-a", "今どうなってる？")

        assert runtime.observation_store.all() == []
        assert runtime.state_delta_store.all() == []
        assert not any(
            "順調です。" in json.dumps(entry.value, ensure_ascii=False, default=str)
            for entry in runtime.state_store.all_current()
        )
        situation = runtime.services.get_project_situation("project-a")
        assert not any(
            "順調です。" in json.dumps(item, ensure_ascii=False, default=str)
            for item in situation.recent_changes
        )
    finally:
        runtime.close()


async def test_pending_review_reaches_the_answer_context(tmp_path):
    backend = _backend(_answer("Reviewを待っています。"))
    runtime = _project_runtime(tmp_path, backend=backend)
    try:
        work = runtime.work_requirement_store.get_by_work_key("project-a:active")
        process = ProcessInstance(
            "review_human_work",
            "1",
            status=ProcessStatus.SUSPENDED,
            work_requirement_id=work.id,
        )
        runtime.process_store.save_instance(process)
        runtime.continuation_store.save(
            Continuation(
                process_instance_id=process.id,
                resume_point="await_work_review",
                waiting_for={
                    "event_type": "work_reviewed",
                    "work_requirement_id": str(work.id),
                },
            )
        )

        await ProjectChatService(runtime).ask("project-a", "次に何を確認すべき？")

        situation = backend.calls[-1].context["project_situation"]
        assert situation["pending_reviews"][0]["event_type"] == "work_reviewed"
    finally:
        runtime.close()


async def test_invented_references_are_dropped_from_an_answer(tmp_path):
    runtime = _project_runtime(tmp_path)
    work = runtime.work_requirement_store.get_by_work_key("project-a:blocked")
    runtime.register_backend(
        "llm",
        _backend(
            _answer(
                "レポート作成が停止しています。",
                references=[str(work.id), "made-up-identifier"],
            )
        ),
    )
    try:
        reply = await ProjectChatService(runtime).ask("project-a", "何で止まってる？")
        assert reply["references"] == [str(work.id)]
    finally:
        runtime.close()


async def test_unknown_certainty_is_never_reported_as_fact(tmp_path):
    runtime = _project_runtime(
        tmp_path,
        backend=_backend({"answer": "たぶん停止しています。", "certainty": "sure"}),
    )
    try:
        reply = await ProjectChatService(runtime).ask("project-a", "何で止まってる？")
        assert reply["status"] == ChatAnswerStatus.ANSWERED.value
        assert reply["certainty"] == "INFERENCE"
    finally:
        runtime.close()


async def test_chat_http_api_reuses_cockpit_authentication(tmp_path):
    runtime = _project_runtime(
        tmp_path, backend=_backend(_answer("レポート作成が停止しています。"))
    )
    cockpit = CockpitService(runtime, phase6_enabled=False, master_id="operator")
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN), cockpit=cockpit
    ).start()
    port = server.bound_port
    try:
        status, body = await _request(
            port, "POST", "/projects/project-a/chat", token=None, body={"message": "?"}
        )
        assert status == 401 and body["error"] == "unauthorized"

        status, body = await _request(
            port, "POST", "/projects/project-a/chat", body={"message": "何で止まってる？"}
        )
        assert status == 200
        assert body["answer"] == "レポート作成が停止しています。"
        assert body["project_id"] == "project-a"
        assert body["status"] == "ANSWERED"

        status, history = await _request(port, "GET", "/projects/project-a/chat")
        assert status == 200
        assert history["thread_id"] == body["thread_id"]
        assert [item["text"] for item in history["messages"]] == [
            "何で止まってる？",
            "レポート作成が停止しています。",
        ]

        status, body = await _request(
            port, "POST", "/projects/project-a/chat", body={"message": "   "}
        )
        assert status == 400

        status, body = await _request(
            port, "POST", "/projects/unknown/chat", body={"message": "状況は？"}
        )
        assert status == 404 and body["error"] == "project not found"

        status, body = await _request(port, "GET", "/projects/unknown/chat")
        assert status == 404 and body["error"] == "project not found"
    finally:
        await server.stop()
        runtime.close()


async def test_chat_routes_disappear_with_cockpit_but_runtime_keeps_running(tmp_path):
    runtime = _project_runtime(tmp_path, backend=_backend(_answer("ok")))
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN), cockpit=None
    ).start()
    try:
        status, body = await _request(
            server.bound_port,
            "POST",
            "/projects/project-a/chat",
            body={"message": "状況は？"},
        )
        assert status == 404 and body["error"] == "cockpit is disabled"

        status, body = await _request(
            server.bound_port,
            "POST",
            "/ingress/webhook",
            body={
                "event_type": "human_message",
                "source_event_key": "chat-disabled-1",
                "payload": {"text": "runtime still accepts events"},
            },
        )
        assert status == 202 and body["status"] == "accepted"
    finally:
        await server.stop()
        runtime.close()


def test_cockpit_asset_exposes_the_read_only_chat_panel():
    assert 'data-view="projects"' in APP_JS or "projects:renderProjects" in APP_JS
    assert "chat-form" in APP_JS
    assert "回答を生成しています" in APP_JS
    assert "LLM未接続" in APP_JS
