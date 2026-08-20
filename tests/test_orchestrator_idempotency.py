"""An instruction delivered twice must not be carried out twice.

Delegating is not free: a duplicated instruction hands the Agent the same task
again, and the Agent may act on the outside world.  A caller that supplies a
``request_id`` gets exactly-once handling — the resend replays the recorded
decision instead of routing, spawning or delegating again.
"""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends import proposal_response
from nexus_seed.cockpit import CockpitService
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.runtime.runtime import Runtime

TOKEN = "idem-token"
REQUEST = "7月の売上低下原因を調べて"


def decision(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


class ScriptedBackend:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def execute(self, request):
        self.calls.append(request)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def quiet(config, envelope):
    return []


async def _setup(tmp_path, name="idem.db"):
    runtime = Runtime(tmp_path / name)
    backend = ScriptedBackend([decision("CREATE_PROJECT", proposed_goal=REQUEST)])
    agent_runtime = InProcessAgentRuntime(behaviour=quiet)
    orch = ProjectOrchestrator(runtime.db, agent_runtime=agent_runtime, backend=backend)
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    project = orch.projects.all()[0]
    backend.results = [
        decision("ADD_TASK_TO_PROJECT", target_project_id=project.id,
                 proposed_task="地域別の内訳も出す")
    ]
    return runtime, orch, project, backend, agent_runtime


async def test_same_request_id_is_carried_out_once(tmp_path):
    runtime, orch, project, backend, agent_runtime = await _setup(tmp_path)
    service = CockpitService(runtime, phase6_enabled=True, master_id="op")
    routed_before = len(backend.calls)
    delivered_before = len(agent_runtime.delivered)

    first = await service.orchestrator_instruct(project.id, "地域別も", request_id="click-1")
    second = await service.orchestrator_instruct(project.id, "地域別も", request_id="click-1")
    third = await service.orchestrator_instruct(project.id, "地域別も", request_id="click-1")

    # One task, one routing call, one delegation to the Agent.
    assert len(orch.projects.get(project.id).tasks) == 1
    assert len(backend.calls) == routed_before + 1
    assert len(agent_runtime.delivered) == delivered_before + 1
    # Every delivery answers the same thing, so a retry is safe for the caller.
    assert first["decision"] == second["decision"] == third["decision"]
    assert first["affected_project_id"] == second["affected_project_id"]
    runtime.close()


async def test_a_different_request_id_is_new_work(tmp_path):
    runtime, orch, project, backend, _ = await _setup(tmp_path)
    service = CockpitService(runtime, phase6_enabled=True, master_id="op")

    await service.orchestrator_instruct(project.id, "地域別も", request_id="click-1")
    await service.orchestrator_instruct(project.id, "商品別も", request_id="click-2")

    assert len(orch.projects.get(project.id).tasks) == 2
    runtime.close()


async def test_without_a_request_id_behaviour_is_unchanged(tmp_path):
    runtime, orch, project, _backend, _ = await _setup(tmp_path)
    service = CockpitService(runtime, phase6_enabled=True, master_id="op")

    await service.orchestrator_instruct(project.id, "地域別も")
    await service.orchestrator_instruct(project.id, "地域別も")

    # No key, no promise: two deliveries are two instructions, as before.
    assert len(orch.projects.get(project.id).tasks) == 2
    runtime.close()


async def test_replay_survives_a_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    runtime, orch, project, backend, _ = await _setup(tmp_path, name="restart.db")
    service = CockpitService(runtime, phase6_enabled=True, master_id="op")
    await service.orchestrator_instruct(project.id, "地域別も", request_id="click-1")
    assert len(orch.projects.get(project.id).tasks) == 1
    runtime.close()

    # The ledger is durable, so the same delivery after a restart still replays.
    runtime2 = Runtime(db_path)
    backend2 = ScriptedBackend([
        decision("ADD_TASK_TO_PROJECT", target_project_id=project.id,
                 proposed_task="地域別の内訳も出す")
    ])
    orch2 = ProjectOrchestrator(
        runtime2.db, agent_runtime=InProcessAgentRuntime(behaviour=quiet), backend=backend2
    )
    bootstrap_project_orchestration(runtime2, orch2, enabled=True)
    service2 = CockpitService(runtime2, phase6_enabled=True, master_id="op")

    await service2.orchestrator_instruct(project.id, "地域別も", request_id="click-1")

    assert len(orch2.projects.get(project.id).tasks) == 1
    assert backend2.calls == []  # not routed again
    runtime2.close()


async def test_submit_is_the_place_the_guarantee_lives(tmp_path):
    """Any caller of submit() gets it, not only the Cockpit."""
    runtime, orch, project, _backend, _ = await _setup(tmp_path)

    await orch.submit("直接", origin_project_id=project.id, request_id="cli-1")
    await orch.submit("直接", origin_project_id=project.id, request_id="cli-1")

    assert len(orch.projects.get(project.id).tasks) == 1
    runtime.close()


# --- HTTP --------------------------------------------------------------------


async def _post(port, path, body, token=TOKEN):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"POST {path} HTTP/1.1", "Host: 127.0.0.1"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    lines += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, raw = response.partition(b"\r\n\r\n")
    return int(head.split(b"\r\n", 1)[0].split()[1]), json.loads(raw)


async def test_http_retry_with_the_same_request_id_is_safe(tmp_path):
    runtime, orch, project, _backend, _ = await _setup(tmp_path)
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        host="127.0.0.1",
        port=0,
        cockpit=CockpitService(runtime, phase6_enabled=True, master_id="op"),
    )
    await server.start()
    path = f"/cockpit/api/orchestrator/projects/{project.id}/instruct"
    try:
        body = {"message": "地域別も", "request_id": "http-1"}
        status1, first = await _post(server.bound_port, path, body)
        status2, second = await _post(server.bound_port, path, body)

        assert status1 == status2 == 200
        assert first["decision"] == second["decision"]
        assert len(orch.projects.get(project.id).tasks) == 1
    finally:
        await server.stop()
        runtime.close()


def test_cockpit_sends_a_stable_request_id():
    # The key is minted once per message and only cleared on success, so a
    # retry after a lost response replays instead of repeating.
    assert "messageKey" in APP_JS
    assert "request_id:o.messageKey" in APP_JS
    assert "o.messageKey=null" in APP_JS
