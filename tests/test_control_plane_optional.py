"""NEXUS SEED runs without the Control Plane.

The Phase 5G human command surface is being wound down in favour of the Project
Orchestrator.  Turning it off removes the command surface — /control, the
Cockpit's control actions, the Control Plane instruction box — and must leave
the Runtime, the durable records and the orchestrator untouched.
"""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends import proposal_response
from nexus_seed.chat.instruct import ProjectInstructionService
from nexus_seed.chat.models import ChatAnswerStatus
from nexus_seed.cockpit import CockpitService
from nexus_seed.control.models import Goal
from nexus_seed.core.event import Event
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator, ProjectStatus
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.runtime.runtime import Runtime

TOKEN = "no-control"


def decision(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


class Scripted:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, request):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def test_control_plane_is_on_by_default(tmp_path):
    runtime = Runtime(tmp_path / "on.db")
    assert runtime.console is not None
    runtime.close()


def test_disabling_it_removes_only_the_command_surface(tmp_path):
    runtime = Runtime(tmp_path / "off.db", control_enabled=False)

    assert runtime.console is None
    # The durable records and every other store are still there: this switches
    # off the human command surface, not the data behind it.
    assert runtime.control_store is not None
    assert runtime.control_store.goals() == []
    assert runtime.state_store is not None
    assert runtime.work_requirement_store is not None
    runtime.close()


async def test_the_orchestrator_works_with_the_control_plane_off(tmp_path):
    runtime = Runtime(tmp_path / "orch.db", control_enabled=False)
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=lambda c, e: []),
        backend=Scripted([decision("CREATE_PROJECT", proposed_goal="調査する")]),
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)

    await orch.handle_request("調べて")

    [project] = orch.projects.all()
    assert project.status is ProjectStatus.ACTIVE
    assert project.assigned_agent_id is not None
    runtime.close()


async def test_human_messages_still_become_projects(tmp_path):
    """The ordinary door — Ingress -> human_message -> router — is unaffected."""
    runtime = Runtime(tmp_path / "ingress.db", control_enabled=False)
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=lambda c, e: []),
        backend=Scripted([decision("CREATE_PROJECT", proposed_goal="調査する")]),
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)

    await runtime.submit_event(Event("human_message", "user", {"text": "調べて"}))

    assert len(orch.projects.all()) == 1
    runtime.close()


def test_cockpit_reports_control_as_unavailable(tmp_path):
    runtime = Runtime(tmp_path / "cockpit.db", control_enabled=False)
    service = CockpitService(
        runtime, phase6_enabled=True, master_id="op",
        control_enabled=runtime.console is not None,
    )

    snapshot = service.snapshot()

    assert snapshot["control"]["enabled"] is False
    # Reading a project still works; only acting on it is gone.
    assert "projects" in snapshot
    runtime.close()


async def test_the_control_plane_instruction_box_refuses_cleanly(tmp_path):
    runtime = Runtime(tmp_path / "instruct.db", control_enabled=False)
    goal = Goal(title="A", objective="o", owner_identity_id="op",
                metadata={"project_id": "project-a", "project_title": "A"})
    runtime.control_store.save_goal(goal)
    service = ProjectInstructionService(runtime, runtime.console)

    assert service.available is False
    result = await service.instruct(
        "project-a", '/task create objective="x"', issuer_identity_id="op"
    )

    # It explains itself instead of failing, and changes nothing.
    assert result["status"] == ChatAnswerStatus.INSTRUCTION_REFUSED.value
    assert "Control Plane" in result["answer"]
    assert runtime.work_requirement_store.all() == []
    runtime.close()


async def test_control_endpoint_is_gone(tmp_path):
    runtime = Runtime(tmp_path / "http.db", control_enabled=False)
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        host="127.0.0.1",
        port=0,
        cockpit=CockpitService(runtime, phase6_enabled=True, master_id="op"),
        console=runtime.console,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        payload = json.dumps({"command": "/status"}).encode()
        writer.write(
            (
                "POST /control HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
            ).encode("latin-1")
            + payload
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        status = int(response.split(b"\r\n", 1)[0].split()[1])
        assert status == 404
    finally:
        await server.stop()
        runtime.close()
