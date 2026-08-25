"""The Cockpit shows what an Agent asked for, and lets a person answer it.

The decision itself is tested in tests/test_workspace_grants.py; these tests
cover the operator's path: a NEED_RESOURCE escalation has to be visible as a
resource request rather than a generic "stuck", and granting from the Cockpit
has to reach the same GrantPolicy the rest of the system uses.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.cockpit import CockpitService
from nexus_seed.orchestrator import (
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    escalation,
    status_message,
)
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.resources.scope import ResourceScope
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.workspace.policy import GrantPolicy

REQUEST = "共通仕様に合わせて計画を直して"


def asks_for_a_file(config, envelope):
    return [
        status_message(config.project_id, "workspaceに仕様がない"),
        escalation(
            config.project_id,
            A2AMessageType.NEED_RESOURCE,
            required_resource="共通仕様 spec.md",
            reason="計画を直すには共通仕様が要る",
        ),
    ]


async def blocked_runtime(tmp_path, *, writable=False):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "spec.md").write_text("共通仕様", encoding="utf-8")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "keys.txt").write_text("TOP SECRET", encoding="utf-8")

    runtime = Runtime(tmp_path / "app.db")
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=asks_for_a_file),
        backend=FakeLLMBackend(
            default=proposal_response(
                {
                    "action": "CREATE_PROJECT",
                    "proposed_goal": REQUEST,
                    "reason": "new",
                    "confidence": 0.9,
                }
            )
        ),
        workspace_root=str(tmp_path / "workspaces"),
        grant_policy=GrantPolicy(
            scope=ResourceScope(
                read_roots=[shared],
                write_roots=[shared] if writable else [],
                create=False,
            )
        ),
    )
    bootstrap_project_orchestration(runtime, orch)
    await orch.handle_request(REQUEST)
    return runtime, orch, orch.projects.all()[0], shared, outside


async def test_a_resource_request_is_shown_as_a_request_not_as_stuck(tmp_path):
    runtime, _orch, project, _shared, _outside = await blocked_runtime(tmp_path)

    items = CockpitService(runtime, master_id="local-operator").snapshot()["needs_attention"]
    requests = [item for item in items if item["kind"] == "resource_request"]

    assert [item["target_id"] for item in requests] == [project.id]
    assert "共通仕様" in requests[0]["message"]
    # The generic "this project is stuck" item would tell a person nothing
    # about what to do, so it must not also be raised for the same blocker.
    assert not [item for item in items if item["kind"] == "project_blocked"]


async def test_granting_write_from_the_cockpit_gives_the_agent_a_copy(tmp_path):
    runtime, _orch, project, shared, _outside = await blocked_runtime(
        tmp_path, writable=True
    )
    cockpit = CockpitService(runtime, master_id="local-operator")

    result = await cockpit.orchestrator_grant(
        project.id, f"file:{shared / 'spec.md'}", access="read_write", reason="人が許可"
    )

    assert result["decision"]["allowed"] is True
    assert [item["uri"] for item in result["granted"]] == [f"file:{shared / 'spec.md'}"]
    copy = tmp_path / "workspaces" / project.id / "resources" / "spec.md"
    assert copy.read_text(encoding="utf-8") == "共通仕様"
    assert result["writable_paths"] == [str(copy)]


async def test_granting_by_reference_hands_over_the_real_path(tmp_path):
    runtime, _orch, project, shared, _outside = await blocked_runtime(tmp_path)
    cockpit = CockpitService(runtime, master_id="local-operator")

    result = await cockpit.orchestrator_grant(project.id, f"file:{shared / 'spec.md'}")

    assert result["decision"]["allowed"] is True
    assert result["readable_paths"] == [str(shared / "spec.md")]
    assert result["writable_paths"] == []
    assert not (tmp_path / "workspaces" / project.id / "resources" / "spec.md").exists()


async def test_the_cockpit_cannot_hand_over_a_file_outside_the_authorized_root(tmp_path):
    runtime, _orch, project, _shared, outside = await blocked_runtime(tmp_path)
    cockpit = CockpitService(runtime, master_id="local-operator")

    result = await cockpit.orchestrator_grant(project.id, f"file:{outside / 'keys.txt'}")

    assert result["decision"]["allowed"] is False
    assert result["granted"] == []
    workspace = tmp_path / "workspaces" / project.id
    assert not list(workspace.rglob("keys.txt"))


async def test_granting_an_unknown_project_is_a_miss_not_an_error(tmp_path):
    runtime, _orch, _project, shared, _outside = await blocked_runtime(tmp_path)
    cockpit = CockpitService(runtime, master_id="local-operator")

    assert await cockpit.orchestrator_grant("project-nope", f"file:{shared}") is None


# --- HTTP -------------------------------------------------------------------


async def test_http_grant_reaches_the_same_policy(tmp_path):
    import asyncio
    import json

    from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer

    token = "cockpit-token"
    runtime, _orch, project, shared, outside = await blocked_runtime(tmp_path)
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=token),
        host="127.0.0.1",
        port=0,
        cockpit=CockpitService(runtime, master_id="local-operator"),
    )
    await server.start()

    async def post(path, body):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = "\r\n".join(
            [
                f"POST {path} HTTP/1.1",
                "Host: 127.0.0.1",
                f"Authorization: Bearer {token}",
                "Content-Type: application/json",
                f"Content-Length: {len(payload)}",
            ]
        )
        writer.write((head + "\r\n\r\n").encode("latin-1") + payload)
        await writer.drain()
        response = await reader.read()
        writer.close()
        raw_head, _, raw = response.partition(b"\r\n\r\n")
        return int(raw_head.split(b"\r\n", 1)[0].split()[1]), json.loads(raw)

    base = f"/cockpit/api/orchestrator/projects/{project.id}"
    try:
        status, payload = await post(f"{base}/grant", {"uri": ""})
        assert status == 400

        status, payload = await post(f"{base}/grant", {"uri": f"file:{outside / 'keys.txt'}"})
        assert status == 200
        assert payload["decision"]["allowed"] is False

        status, payload = await post(f"{base}/grant", {"uri": f"file:{shared / 'spec.md'}"})
        assert status == 200
        assert payload["decision"]["allowed"] is True

        status, _ = await post(
            "/cockpit/api/orchestrator/projects/project-nope/grant",
            {"uri": f"file:{shared / 'spec.md'}"},
        )
        assert status == 404
    finally:
        await server.stop()
        runtime.close()


def test_the_cockpit_page_offers_the_grant_action():
    from nexus_seed.cockpit.assets import APP_JS

    assert "grant-orchestrator-resource" in APP_JS
    assert "resource_request" in APP_JS
