"""NEXUS SEED has no human command surface, and does not need one.

/control, the slash-command parser, ConsoleService and the Control Plane
instruction box are gone.  People now instruct the Project Orchestrator, decide
reviews and answer self questions directly.  These tests pin the removal — that
nothing still imports the command vocabulary, and that everything the command
surface used to gate still works without it.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends import proposal_response
from nexus_seed.cockpit import CockpitService
from nexus_seed.core.event import Event
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator, ProjectStatus
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.runtime.runtime import Runtime

pytestmark = pytest.mark.asyncio

TOKEN = "no-control"


def decision(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


class Scripted:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, request):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


async def test_the_command_modules_no_longer_exist():
    for name in (
        "nexus_seed.control.service",
        "nexus_seed.control.parser",
        "nexus_seed.control.adapters",
        "nexus_seed.chat.instruct",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


async def test_a_runtime_has_no_console(tmp_path):
    runtime = Runtime(tmp_path / "runtime.db")
    try:
        assert not hasattr(runtime, "console")
        assert not hasattr(runtime, "control_store")
        assert runtime.state_store is not None
        assert runtime.work_requirement_store is not None
    finally:
        runtime.close()


async def test_the_orchestrator_is_the_way_in(tmp_path):
    runtime = Runtime(tmp_path / "orch.db")
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=lambda c, e: []),
        backend=Scripted([decision("CREATE_PROJECT", proposed_goal="調査する")]),
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    try:
        await orch.handle_request("調べて")
        [project] = orch.projects.all()
        assert project.status is ProjectStatus.ACTIVE
        assert project.assigned_agent_id is not None
    finally:
        runtime.close()


async def test_human_messages_still_become_projects(tmp_path):
    """The ordinary door — Ingress -> human_message -> router — is unaffected."""

    runtime = Runtime(tmp_path / "ingress.db")
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=lambda c, e: []),
        backend=Scripted([decision("CREATE_PROJECT", proposed_goal="調査する")]),
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    try:
        await runtime.submit_event(Event("human_message", "user", {"text": "調べて"}))
        assert len(orch.projects.all()) == 1
    finally:
        runtime.close()


async def test_the_cockpit_no_longer_advertises_a_control_plane(tmp_path):
    runtime = Runtime(tmp_path / "cockpit.db")
    try:
        snapshot = CockpitService(
            runtime, phase6_enabled=True, master_id="op"
        ).snapshot()
        assert "control" not in snapshot
        # Reading projects still works; acting on one goes to the Orchestrator.
        assert "orchestrator" in snapshot
    finally:
        runtime.close()


async def test_the_control_endpoint_is_gone(tmp_path):
    runtime = Runtime(tmp_path / "http.db")
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        host="127.0.0.1",
        port=0,
        cockpit=CockpitService(runtime, phase6_enabled=True, master_id="op"),
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


async def test_the_cockpit_page_has_no_command_channel_left():
    from nexus_seed.cockpit.assets import APP_JS

    assert "sendCommand" not in APP_JS
    assert '"/control"' not in APP_JS
    assert "New Goal" not in APP_JS
    # What replaced it.
    assert "/cockpit/api/reviews/" in APP_JS
    assert "/cockpit/api/questions/" in APP_JS
    assert "orchestrator-instruct-form" in APP_JS
