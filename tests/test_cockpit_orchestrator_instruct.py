"""Instructing an orchestrator Project, and unblocking one, from the Cockpit.

The Control Plane instruction box needs an allow-list and a scope check because
it executes control commands.  This side needs neither: the ProjectRouter
already decides whether an instruction is more work for this Project or an
independent Goal, and NEXUS SEED only ever creates/extends a Project and hands
it to an Agent.
"""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.cockpit import CockpitService
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.orchestrator import (
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    completed_message,
    escalation,
)
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.runtime.runtime import Runtime

TOKEN = "orch-token"
REQUEST = "7月の売上低下原因を調べて"


def decision(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


class ScriptedBackend:
    """Answers each routing call in turn, so instructions can differ."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def execute(self, request):
        self.calls.append(request)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def cockpit(runtime) -> CockpitService:
    return CockpitService(runtime, phase6_enabled=True, master_id="local-operator")


async def _setup(tmp_path, behaviour, backend, name="orch.db"):
    runtime = Runtime(tmp_path / name)
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=backend,
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    return runtime, orch, orch.projects.all()[0]


def _blocking(config, envelope):
    return [
        escalation(
            config.project_id,
            A2AMessageType.NEED_CAPABILITY,
            required_capability="sap",
            reason="SAP accessがない",
        )
    ]


# --- instructing --------------------------------------------------------------


async def test_instruction_adds_a_task_to_the_same_project(tmp_path):
    quiet = lambda config, envelope: []
    backend = ScriptedBackend(
        [decision("CREATE_PROJECT", proposed_goal=REQUEST), None]
    )
    runtime, orch, project = await _setup(tmp_path, quiet, backend)
    # The next routing answer keeps the work on this project.
    backend.results = [
        decision(
            "ADD_TASK_TO_PROJECT",
            target_project_id=project.id,
            proposed_task="地域別の内訳も出す",
        )
    ]

    result = await cockpit(runtime).orchestrator_instruct(project.id, "地域別の内訳も出して")

    assert result["decision"]["action"] == "ADD_TASK_TO_PROJECT"
    assert result["affected_project_id"] == project.id
    # No second project, and the task is on this one.
    assert len(orch.projects.all()) == 1
    assert [t["description"] for t in result["project"]["tasks"]] == ["地域別の内訳も出す"]
    # The router was told which project the instruction came from.
    assert backend.calls[-1].context["origin_project_id"] == project.id
    assert backend.calls[-1].context["source"] == "cockpit-instruct"
    runtime.close()


async def test_independent_instruction_becomes_a_child_project(tmp_path):
    quiet = lambda config, envelope: []
    backend = ScriptedBackend([decision("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await _setup(tmp_path, quiet, backend)
    backend.results = [decision("CREATE_PROJECT", proposed_goal="データ品質を是正する")]

    result = await cockpit(runtime).orchestrator_instruct(project.id, "データがおかしい")

    assert result["decision"]["action"] == "CREATE_PROJECT"
    children = orch.projects.children_of(project.id)
    assert [c.goal for c in children] == ["データ品質を是正する"]
    # A child Project gets its own Agent (1 Project = 1 Agent).
    assert children[0].assigned_agent_id != project.assigned_agent_id
    runtime.close()


async def test_instruction_on_unknown_project_is_none(tmp_path):
    quiet = lambda config, envelope: []
    backend = ScriptedBackend([decision("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, _orch, _project = await _setup(tmp_path, quiet, backend)

    assert await cockpit(runtime).orchestrator_instruct("project-nope", "何か") is None
    runtime.close()


async def test_empty_instruction_is_rejected(tmp_path):
    quiet = lambda config, envelope: []
    backend = ScriptedBackend([decision("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, _orch, project = await _setup(tmp_path, quiet, backend)

    try:
        await cockpit(runtime).orchestrator_instruct(project.id, "   ")
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty instruction should be rejected")
    runtime.close()


# --- unblocking ----------------------------------------------------------------


async def test_unblock_clears_blockers_and_hands_the_project_back(tmp_path):
    calls = {"n": 0}

    def behaviour(config, envelope):
        calls["n"] += 1
        if calls["n"] == 1:
            return _blocking(config, envelope)
        return [completed_message(config.project_id, "SAPが使えたので完了")]

    backend = ScriptedBackend([decision("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await _setup(tmp_path, behaviour, backend)
    assert orch.projects.get(project.id).status is ProjectStatus.BLOCKED

    result = await cockpit(runtime).orchestrator_unblock(project.id, "SAP権限を付与した")

    assert result["project"]["status"] == ProjectStatus.COMPLETED.value
    # Blockers are history, not control state: resolved and attributed, never
    # deleted, so why the project stopped is still readable afterwards.
    [blocker] = orch.projects.get(project.id).blockers
    assert blocker["kind"] == "NEED_CAPABILITY"
    assert blocker["resolved_at"]
    assert blocker["resolved_by"] == "resolved"
    assert result["project"]["blocker_history"] == [blocker]
    runtime.close()


async def test_unblock_without_orchestrator_is_none(tmp_path):
    runtime = Runtime(tmp_path / "no-orch.db")  # orchestrator never attached

    assert await cockpit(runtime).orchestrator_unblock("project-a") is None
    assert await cockpit(runtime).orchestrator_instruct("project-a", "x") is None
    runtime.close()


# --- HTTP -----------------------------------------------------------------------


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


async def test_http_instruct_and_unblock(tmp_path):
    calls = {"n": 0}

    def behaviour(config, envelope):
        calls["n"] += 1
        if calls["n"] == 1:
            return _blocking(config, envelope)
        return [completed_message(config.project_id, "権限が付いたので完了")]

    backend = ScriptedBackend([decision("CREATE_PROJECT", proposed_goal=REQUEST)])
    runtime, orch, project = await _setup(tmp_path, behaviour, backend)
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        host="127.0.0.1",
        port=0,
        cockpit=cockpit(runtime),
    )
    await server.start()
    port = server.bound_port
    base = f"/cockpit/api/orchestrator/projects/{project.id}"
    try:
        status, _ = await _request(port, "POST", f"{base}/instruct", token=None,
                                   body={"message": "x"})
        assert status == 401

        status, _ = await _request(port, "POST", f"{base}/instruct", body={"message": "  "})
        assert status == 400

        status, _ = await _request(port, "POST", f"{base}/nope", body={"message": "x"})
        assert status == 404

        backend.results = [
            decision("ADD_TASK_TO_PROJECT", target_project_id=project.id,
                     proposed_task="追加調査")
        ]
        status, payload = await _request(port, "POST", f"{base}/instruct",
                                         body={"message": "追加で調べて"})
        assert status == 200
        assert payload["decision"]["action"] == "ADD_TASK_TO_PROJECT"

        status, payload = await _request(port, "POST", f"{base}/unblock",
                                         body={"note": "権限付与"})
        assert status == 200
        assert payload["project"]["status"] == ProjectStatus.COMPLETED.value
        assert all(b["resolved_at"] for b in payload["project"]["blocker_history"])

        status, _ = await _request(
            port, "POST", "/cockpit/api/orchestrator/projects/project-nope/unblock", body={}
        )
        assert status == 404
    finally:
        await server.stop()
        runtime.close()


def test_cockpit_renders_the_project_thread_and_unblock():
    # The instruct endpoint tested above is still the API; the page reaches it
    # through the project thread's single box.
    assert "project-message-form" in APP_JS
    assert "unblock-orchestrator-project" in APP_JS
    assert "ブロック解除してAgentに戻す" in APP_JS
