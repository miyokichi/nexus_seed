"""One box per project: NEXUS SEED decides whether you asked or told it.

The person should not have to classify their own message before typing it.
These tests pin that the same input reaches an answer or the Agent depending
on what it means, that both land in one thread, and that a runtime which
cannot judge meaning errs towards explaining rather than delegating.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends.llm import FakeLLMBackend, proposal_response
from nexus_seed.chat.message import ASKED, TOLD
from nexus_seed.cockpit import CockpitService
from nexus_seed.orchestrator import (
    InProcessAgentRuntime,
    ProjectOrchestrator,
    RoutingAction,
)
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.runtime.runtime import Runtime


pytestmark = pytest.mark.asyncio

TOKEN = "message-token"
REQUEST = "7月の売上低下原因を調べて"

quiet = lambda config, envelope: []


def routing(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


def answer(text: str) -> dict:
    return proposal_response({"answer": text, "certainty": "FACT", "references": []})


class Scripted:
    """Answers each call in turn; the router and the chat share one backend."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def execute(self, request):
        self.calls.append(request)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


async def project_runtime(tmp_path, backend, name="message.db"):
    runtime = Runtime(tmp_path / name)
    # The router and Project Chat share one backend here, as they do in the
    # shipped app: both read the same message, for different questions.
    runtime.register_backend("llm", backend)
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=quiet),
        backend=backend,
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    return runtime, orch, orch.projects.all()[0]


def cockpit(runtime) -> CockpitService:
    return CockpitService(runtime, phase6_enabled=False, master_id="operator")


async def test_a_question_is_answered_and_nothing_is_delegated(tmp_path):
    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await project_runtime(tmp_path, backend)
    try:
        backend.results = [routing("IGNORE"), answer("SAP accessがなくて止まっています。")]

        result = await cockpit(runtime).project_message(project.id, "今どうなってる？")

        assert result["kind"] == ASKED
        assert result["answer"] == "SAP accessがなくて止まっています。"
        assert result["routing"]["action"] == RoutingAction.IGNORE.value
        # Asking delegated nothing.
        assert orch.projects.get(project.id).tasks == []
        assert len(orch.projects.all()) == 1
    finally:
        runtime.close()


async def test_an_instruction_reaches_the_agent(tmp_path):
    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await project_runtime(tmp_path, backend, "instruct.db")
    try:
        backend.results = [
            routing(
                "ADD_TASK_TO_PROJECT",
                target_project_id=project.id,
                proposed_task="地域別の内訳も出す",
            )
        ]

        result = await cockpit(runtime).project_message(project.id, "地域別の内訳も出して")

        assert result["kind"] == TOLD
        assert result["affected_project_id"] == project.id
        assert "地域別の内訳も出す" in result["answer"]
        tasks = orch.projects.get(project.id).tasks
        assert [item["description"] for item in tasks] == ["地域別の内訳も出す"]
    finally:
        runtime.close()


async def test_both_land_in_the_same_thread(tmp_path):
    """The point of one box is one history, not two half-conversations."""

    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, _orch, project = await project_runtime(tmp_path, backend, "thread.db")
    service = cockpit(runtime)
    try:
        backend.results = [routing("IGNORE"), answer("止まっています。")]
        asked = await service.project_message(project.id, "今どうなってる？")

        backend.results = [
            routing(
                "ADD_TASK_TO_PROJECT",
                target_project_id=project.id,
                proposed_task="内訳を出す",
            )
        ]
        told = await service.project_message(project.id, "内訳も出して")

        assert asked["thread_id"] == told["thread_id"]
        history = service.project_chat_history(project.id)["messages"]
        assert [item["text"] for item in history] == [
            "今どうなってる？",
            "止まっています。",
            "内訳も出して",
            "このProjectのTaskとしてAgentに渡しました。\n内訳を出す",
        ]
        assert [item["role"] for item in history] == [
            "HUMAN",
            "NEXUS_SEED",
            "HUMAN",
            "NEXUS_SEED",
        ]
        assert history[-1]["metadata"]["kind"] == "INSTRUCTION"
    finally:
        runtime.close()


async def test_a_message_is_delivered_once(tmp_path):
    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await project_runtime(tmp_path, backend, "once.db")
    service = cockpit(runtime)
    try:
        backend.results = [
            routing(
                "ADD_TASK_TO_PROJECT",
                target_project_id=project.id,
                proposed_task="内訳を出す",
            )
        ]
        first = await service.project_message(project.id, "内訳も出して", request_id="r-1")
        again = await service.project_message(project.id, "内訳も出して", request_id="r-1")

        assert first["affected_project_id"] == again["affected_project_id"]
        assert len(orch.projects.get(project.id).tasks) == 1

        await service.project_message(project.id, "内訳も出して", request_id="r-2")
        assert len(orch.projects.get(project.id).tasks) == 2
    finally:
        runtime.close()


async def test_an_independent_request_becomes_its_own_project(tmp_path):
    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await project_runtime(tmp_path, backend, "child.db")
    try:
        backend.results = [routing("CREATE_PROJECT", proposed_goal="来月の予算を作る")]

        result = await cockpit(runtime).project_message(project.id, "来月の予算も作って")

        assert result["kind"] == TOLD
        assert result["affected_project_id"] != project.id
        assert len(orch.projects.all()) == 2
        assert "別のProject" in result["answer"]
    finally:
        runtime.close()


async def test_without_a_model_a_question_is_explained_not_delegated(tmp_path):
    """Nothing can judge meaning, so the safe direction is to answer."""

    runtime = Runtime(tmp_path / "no-llm.db")
    orch = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime(behaviour=quiet), backend=None
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    project = orch.projects.all()[0]
    try:
        result = await cockpit(runtime).project_message(project.id, "今どうなってる？")

        assert result["kind"] == ASKED
        assert orch.projects.get(project.id).tasks == []
        assert len(orch.projects.all()) == 1
    finally:
        runtime.close()


async def test_without_a_model_a_plain_instruction_still_reaches_the_agent(tmp_path):
    runtime = Runtime(tmp_path / "no-llm-act.db")
    orch = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime(behaviour=quiet), backend=None
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    project = orch.projects.all()[0]
    try:
        result = await cockpit(runtime).project_message(project.id, "これを優先して")

        assert result["kind"] == TOLD
        # It stays on this project: an unroutable in-project request must not
        # split one goal across two Agents.
        assert result["affected_project_id"] == project.id
        assert len(orch.projects.all()) == 1
        tasks = orch.projects.get(project.id).tasks
        assert [item["description"] for item in tasks] == ["これを優先して"]
    finally:
        runtime.close()


async def test_the_person_can_overrule_an_answer_and_have_it_carried_out(tmp_path):
    """Either judge will sometimes be wrong, so the person gets the last word."""

    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await project_runtime(tmp_path, backend, "overrule.db")
    service = cockpit(runtime)
    try:
        backend.results = [routing("IGNORE"), answer("それは説明で足ります。")]
        explained = await service.project_message(project.id, "地域別の内訳も出して")
        assert explained["kind"] == ASKED
        assert orch.projects.get(project.id).tasks == []

        told = await service.project_message(
            project.id, "地域別の内訳も出して", act=True, request_id="r-1"
        )

        assert told["kind"] == TOLD
        assert told["affected_project_id"] == project.id
        tasks = orch.projects.get(project.id).tasks
        assert [item["description"] for item in tasks] == ["地域別の内訳も出して"]
        # The router is not asked again: it already read this and was overruled.
        assert told["routing"]["reason"] == "the person sent this as an instruction"

        # And the correction is delivered once.
        await service.project_message(
            project.id, "地域別の内訳も出して", act=True, request_id="r-1"
        )
        assert len(orch.projects.get(project.id).tasks) == 1
    finally:
        runtime.close()


async def test_overruling_works_with_no_model_at_all(tmp_path):
    runtime = Runtime(tmp_path / "overrule-no-llm.db")
    orch = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime(behaviour=quiet), backend=None
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    project = orch.projects.all()[0]
    try:
        result = await cockpit(runtime).project_message(
            project.id, "地域別の内訳も出して", act=True
        )
        assert result["kind"] == TOLD
        tasks = orch.projects.get(project.id).tasks
        assert [item["description"] for item in tasks] == ["地域別の内訳も出して"]
    finally:
        runtime.close()


async def test_a_routed_question_is_not_refused_as_a_change_request(tmp_path):
    """Two judgements over one message could only contradict each other."""

    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, _orch, project = await project_runtime(tmp_path, backend, "double.db")
    try:
        # Reads as a change request to the keyword guard; the router says it
        # needs no work, and the router is the one that read it for meaning.
        backend.results = [routing("IGNORE"), answer("すでに優先されています。")]

        result = await cockpit(runtime).project_message(project.id, "これを優先して")

        assert result["kind"] == ASKED
        assert result["status"] == "ANSWERED"
        assert result["answer"] == "すでに優先されています。"
    finally:
        runtime.close()


async def test_an_unknown_project_is_not_found(tmp_path):
    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, _orch, _project = await project_runtime(tmp_path, backend, "unknown.db")
    try:
        assert await cockpit(runtime).project_message("no-such-project", "?") is None
        with pytest.raises(ValueError):
            await cockpit(runtime).project_message(_project.id, "   ")
    finally:
        runtime.close()


# --- HTTP -----------------------------------------------------------------------


async def request(port, method, path, *, token=TOKEN, body=None):
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


async def test_http_project_message(tmp_path):
    backend = Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await project_runtime(tmp_path, backend, "http.db")
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN), cockpit=cockpit(runtime)
    ).start()
    path = f"/projects/{project.id}/message"
    try:
        status, _ = await request(server.bound_port, "POST", path, token=None,
                                  body={"message": "x"})
        assert status == 401

        status, _ = await request(server.bound_port, "POST", path, body={"message": "  "})
        assert status == 400

        backend.results = [routing("IGNORE"), answer("止まっています。")]
        status, body = await request(server.bound_port, "POST", path,
                                     body={"message": "今どうなってる？"})
        assert status == 200, body
        assert body["kind"] == ASKED

        backend.results = [
            routing(
                "ADD_TASK_TO_PROJECT",
                target_project_id=project.id,
                proposed_task="内訳を出す",
            )
        ]
        status, body = await request(server.bound_port, "POST", path,
                                     body={"message": "内訳も出して", "request_id": "r-1"})
        assert status == 200, body
        assert body["kind"] == TOLD
        # Resending the same delivery must not hand the Agent the task twice.
        status, body = await request(server.bound_port, "POST", path,
                                     body={"message": "内訳も出して", "request_id": "r-1"})
        assert status == 200
        assert len(orch.projects.get(project.id).tasks) == 1

        status, body = await request(server.bound_port, "POST",
                                     "/projects/nope/message", body={"message": "?"})
        assert status == 404
    finally:
        await server.stop()
        runtime.close()


async def test_the_cockpit_renders_one_box():
    from nexus_seed.cockpit.assets import APP_JS

    assert "project-message-form" in APP_JS
    assert "sendProjectMessage" in APP_JS
    # The two it replaced are gone.
    assert "orchestrator-instruct-form" not in APP_JS
    assert "orchestrator-chat-form" not in APP_JS
    # The one correction the merged box needs.
    assert "resend-as-instruction" in APP_JS
    assert "指示として渡す" in APP_JS
