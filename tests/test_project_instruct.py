"""Per-project instruction channel: the chat can now act, within its project."""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends.llm import FakeLLMBackend, failure_response, proposal_response
from nexus_seed.chat.instruct import ProjectInstructionService, project_scope
from nexus_seed.chat.models import ChatAnswerStatus, ChatRole
from nexus_seed.cockpit import CockpitService
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.control.models import Goal, HumanIdentity
from nexus_seed.core.event import Event
from nexus_seed.projects.projections import get_project_situation
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus

TOKEN = "instruct-token"


def _command(text, reason="ok"):
    return proposal_response({"command": text, "reason": reason})


def _runtime(tmp_path, *, backend=None, name="instruct.db") -> Runtime:
    """A runtime with one project holding one active and one blocked Work."""

    runtime = Runtime(tmp_path / name)
    if backend is not None:
        runtime.register_backend("llm", backend)
    runtime.control_store.save_identity(HumanIdentity("operator", "operator", ("command.*",)))
    goal = Goal(
        title="Project A",
        objective="売上低下の原因を明らかにする",
        owner_identity_id="operator",
        metadata={"project_id": "project-a", "project_title": "Project A"},
    )
    runtime.control_store.save_goal(goal)
    source = runtime.event_store.append(Event("deadline_updated", "test", {}))
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
    other = Goal(
        title="Project B",
        objective="別件",
        owner_identity_id="operator",
        metadata={"project_id": "project-b", "project_title": "Project B"},
    )
    runtime.control_store.save_goal(other)
    return runtime


def _service(runtime) -> ProjectInstructionService:
    return ProjectInstructionService(runtime, runtime.console)


# --- scope ------------------------------------------------------------------


def test_project_scope_lists_only_this_project(tmp_path):
    runtime = _runtime(tmp_path)
    scope = project_scope(get_project_situation(runtime, "project-a"))

    assert len(scope["work"]) == 2
    assert scope["goal"]
    other = project_scope(get_project_situation(runtime, "project-b"))
    assert other["work"] == []
    assert not set(scope["work"]) & set(other["work"])
    runtime.close()


# --- natural language -> command --------------------------------------------


async def test_instruction_creates_work_on_this_project(tmp_path):
    backend = FakeLLMBackend(default=_command('/task create objective="地域別の内訳も出す"'))
    runtime = _runtime(tmp_path, backend=backend)

    result = await _service(runtime).instruct(
        "project-a", "地域別の内訳も出して", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_EXECUTED.value
    assert result["command"].startswith("/task")
    # The instruction and its outcome are both in the project's own thread.
    thread = runtime.chat_store.get_thread("project-a")
    messages = runtime.chat_store.messages(thread.id)
    assert [m.role for m in messages] == [ChatRole.HUMAN, ChatRole.NEXUS_SEED]
    assert messages[0].metadata["kind"] == "INSTRUCTION"
    # New Work landed on this project, not somewhere global.
    created = [
        w for w in runtime.work_requirement_store.all() if "地域別" in (w.objective or "")
    ]
    assert len(created) == 1
    runtime.close()


async def test_instruction_pauses_work_of_this_project(tmp_path):
    runtime = _runtime(tmp_path)
    work_id = project_scope(get_project_situation(runtime, "project-a"))["work"][0]
    runtime.register_backend("llm", FakeLLMBackend(default=_command(f"/pause {work_id}")))

    result = await _service(runtime).instruct(
        "project-a", "いったん止めて", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_EXECUTED.value
    assert result["command"] == f"/pause {work_id}"
    runtime.close()


# --- the guarantees ---------------------------------------------------------


async def test_command_targeting_another_project_is_refused(tmp_path):
    runtime = _runtime(tmp_path)
    # A work id that exists, but belongs to project-a, aimed at project-b's thread.
    foreign_work = project_scope(get_project_situation(runtime, "project-a"))["work"][0]
    runtime.register_backend("llm", FakeLLMBackend(default=_command(f"/pause {foreign_work}")))

    result = await _service(runtime).instruct(
        "project-b", "止めて", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_REFUSED.value
    assert "このProject" in result["answer"]
    runtime.close()


async def test_command_outside_the_allow_list_is_refused(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.register_backend("llm", FakeLLMBackend(default=_command("/status")))

    result = await _service(runtime).instruct(
        "project-a", "全体の状態を変えて", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_REFUSED.value
    assert "system.status" in result["answer"]
    runtime.close()


async def test_message_naming_another_project_is_out_of_scope(tmp_path):
    runtime = _runtime(tmp_path, backend=FakeLLMBackend(default=_command("/task create objective=x")))

    result = await _service(runtime).instruct(
        "project-a", "project-b を止めて", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.OUT_OF_SCOPE.value
    runtime.close()


async def test_unmappable_instruction_is_refused_not_guessed(tmp_path):
    backend = FakeLLMBackend(
        default=proposal_response({"command": None, "reason": "対応するコマンドがありません"})
    )
    runtime = _runtime(tmp_path, backend=backend)

    result = await _service(runtime).instruct(
        "project-a", "いい感じにやっておいて", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_REFUSED.value
    assert "対応するコマンド" in result["answer"]
    runtime.close()


async def test_backend_failure_does_not_execute_anything(tmp_path):
    runtime = _runtime(tmp_path, backend=FakeLLMBackend(default=failure_response("timeout")))
    before = len(runtime.work_requirement_store.all())

    result = await _service(runtime).instruct(
        "project-a", "何かして", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_REFUSED.value
    assert len(runtime.work_requirement_store.all()) == before
    runtime.close()


# --- no LLM ------------------------------------------------------------------


async def test_explicit_command_works_without_an_llm(tmp_path):
    runtime = _runtime(tmp_path)  # no backend registered

    result = await _service(runtime).instruct(
        "project-a", '/task create objective="手動で追加"', issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_EXECUTED.value
    runtime.close()


async def test_natural_language_without_an_llm_explains_itself(tmp_path):
    runtime = _runtime(tmp_path)

    result = await _service(runtime).instruct(
        "project-a", "地域別も見て", issuer_identity_id="operator"
    )

    assert result["status"] == ChatAnswerStatus.INSTRUCTION_REFUSED.value
    assert "/task" in result["answer"]
    runtime.close()


async def test_unknown_project_returns_none(tmp_path):
    runtime = _runtime(tmp_path)
    assert await _service(runtime).instruct(
        "project-zzz", "/task create objective=x", issuer_identity_id="operator"
    ) is None
    runtime.close()


# --- HTTP + UI ----------------------------------------------------------------


async def _request(port, method, path, *, token=TOKEN, body=None):
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
    return int(head.split(b"\r\n", 1)[0].split()[1]), json.loads(raw)


async def test_instruct_endpoint_requires_token_and_executes(tmp_path):
    backend = FakeLLMBackend(default=_command('/task create objective="HTTP経由の追加"'))
    runtime = _runtime(tmp_path, backend=backend)
    cockpit = CockpitService(
        runtime, phase6_enabled=True, master_id="operator", control_enabled=True
    )
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        host="127.0.0.1",
        port=0,
        cockpit=cockpit,
        console=runtime.console,
        control_identity_id="operator",
    )
    await server.start()
    port = server.bound_port
    try:
        status, _ = await _request(port, "POST", "/projects/project-a/instruct", token=None,
                                   body={"message": "追加して"})
        assert status == 401

        status, payload = await _request(port, "POST", "/projects/project-a/instruct",
                                         body={"message": "追加して"})
        assert status == 200
        assert payload["status"] == ChatAnswerStatus.INSTRUCTION_EXECUTED.value

        status, _ = await _request(port, "POST", "/projects/project-zzz/instruct",
                                   body={"message": "追加して"})
        assert status == 404

        # The read-only question path is untouched by all of this.
        history_status, history = await _request(port, "GET", "/projects/project-a/chat")
        assert history_status == 200
        assert history["instructions"]["enabled"] is True
        assert len(history["messages"]) == 2
    finally:
        await server.stop()
        runtime.close()


def test_cockpit_renders_an_instruction_form():
    assert "instruct-form" in APP_JS
    assert "指示する" in APP_JS
    # The question form and the instruction form post to different endpoints.
    assert '#chat-form, #instruct-form' in APP_JS
    assert 'sendProject(form.id==="instruct-form"?"instruct":"chat"' in APP_JS
