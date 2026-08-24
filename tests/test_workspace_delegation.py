"""Delegating a task with resources: declare, decide, provide, execute.

    Task -> 必要Resourceを宣言 -> NEXUS SEEDが権限を判断 -> Worker環境へ提供 -> Agentが実行

These tests exercise that whole responsibility split through the real
ProjectOrchestrator, including the loop that matters most: an Agent that asks
for something it was not given, and the *same* task continuing once NEXUS SEED
allows it.
"""

from __future__ import annotations

import json
from pathlib import Path

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AMessage,
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
)
from nexus_seed.providers.project_agent import (
    WORKSPACE_EXTENSION,
    A2AProjectAgentTransport,
)
from nexus_seed.resources.scope import ResourceScope
from nexus_seed.workspace import AccessMode, GrantPolicy, ResourceGrant, with_grant
from nexus_seed.workspace.provisioner import RESOURCES_DIR


def routing(goal="共通仕様に沿って計画を更新する"):
    return proposal_response(
        {
            "action": "CREATE_PROJECT",
            "proposed_goal": goal,
            "reason": "test",
            "confidence": 0.95,
        }
    )


def shared_tree(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    (shared / "spec.md").write_text("共通仕様", encoding="utf-8")
    (shared / "plan.md").write_text("計画v1", encoding="utf-8")
    (shared / "sap.csv").write_text("year,amount\n2025,100\n", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    (private / "keys.txt").write_text("TOP SECRET", encoding="utf-8")
    return shared, private


def build(tmp_path, *, behaviour=None, writable=True):
    shared, private = shared_tree(tmp_path)
    policy = GrantPolicy(
        scope=ResourceScope(
            read_roots=[shared], write_roots=[shared] if writable else []
        )
    )
    orchestrator = ProjectOrchestrator(
        tmp_path / "o.db",
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour)
        if behaviour
        else InProcessAgentRuntime(),
        backend=FakeLLMBackend(default=routing()),
        workspace_root=str(tmp_path / "workspaces"),
        grant_policy=policy,
    )
    return orchestrator, shared, private


def workspace_of(tmp_path, project):
    return Path(tmp_path / "workspaces" / project.id)


# --- declare -> decide -> provide ---------------------------------------------


async def test_a_task_gets_a_workspace_holding_what_it_was_granted(tmp_path):
    orchestrator, shared, _ = build(tmp_path)
    _decision, project = await orchestrator.submit("計画を更新して")

    # Declared for this task, then re-delegated so the workspace is rebuilt.
    orchestrator.projects.set_context(
        project,
        with_grant(
            with_grant(project.context, ResourceGrant(uri="file:spec.md", reason="参照")),
            ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE),
        ),
    )
    await orchestrator.resolve_block(project.id, reactivate=True)

    workspace = workspace_of(tmp_path, project)
    assert (workspace / RESOURCES_DIR / "spec.md").read_text(encoding="utf-8") == "共通仕様"
    assert (workspace / RESOURCES_DIR / "plan.md").read_text(encoding="utf-8") == "計画v1"
    assert (workspace / "RESOURCES.md").is_file()
    orchestrator.close()


async def test_the_agent_is_told_what_it_has_without_being_told_where_from(tmp_path):
    seen: list[dict] = []

    def capture(config, envelope):
        seen.append(
            {"workspace": config.workspace, "resources": list(config.resources)}
        )
        return [
            A2AMessage(
                type=A2AMessageType.PROJECT_STATUS,
                project_id=config.project_id,
                payload={"summary": "ok"},
            )
        ]

    orchestrator, shared, _ = build(tmp_path, behaviour=capture)
    _decision, project = await orchestrator.submit("計画を更新して")
    orchestrator.projects.set_context(
        project, with_grant(project.context, ResourceGrant(uri="file:spec.md"))
    )
    await orchestrator.resolve_block(project.id, reactivate=True)

    [entry] = seen[-1]["resources"]
    assert entry["path"] == "resources/spec.md"
    assert entry["access"] == "read"
    assert str(shared) not in json.dumps(seen[-1], ensure_ascii=False)
    orchestrator.close()


async def test_a_project_without_grants_still_gets_its_own_workspace(tmp_path):
    """The behaviour that predates grants stays exactly as it was."""
    orchestrator, _shared, _ = build(tmp_path)
    _decision, project = await orchestrator.submit("何かして")

    assert orchestrator.granted(project.id) == []
    assert workspace_of(tmp_path, project).is_dir()
    orchestrator.close()


async def test_grants_survive_a_restart(tmp_path):
    orchestrator, _shared, _ = build(tmp_path)
    _decision, project = await orchestrator.submit("計画を更新して")
    await orchestrator.grant_resource(project.id, "file:spec.md", reason="参照")
    orchestrator.close()

    reopened, _shared, _ = build(tmp_path)
    assert [g.uri for g in reopened.granted(project.id)] == ["file:spec.md"]
    reopened.close()


# --- the host is not the workspace ---------------------------------------------


async def test_a_path_outside_the_authorized_root_is_never_granted(tmp_path):
    orchestrator, _shared, private = build(tmp_path)
    _decision, project = await orchestrator.submit("鍵を読んで")

    decision = await orchestrator.grant_resource(
        project.id, f"file:{private / 'keys.txt'}"
    )

    assert decision.allowed is False
    assert orchestrator.granted(project.id) == []
    assert not (workspace_of(tmp_path, project) / RESOURCES_DIR / "keys.txt").exists()
    orchestrator.close()


async def test_a_refusal_leaves_the_project_blocked_with_the_reason(tmp_path):
    orchestrator, _shared, private = build(tmp_path)
    _decision, project = await orchestrator.submit("鍵を読んで")

    await orchestrator.grant_resource(project.id, "file:/etc/passwd")

    blockers = orchestrator.projects.get(project.id).current_blockers
    assert any(b["kind"] == "RESOURCE_REFUSED" for b in blockers)
    orchestrator.close()


async def test_with_no_policy_nothing_is_grantable(tmp_path):
    orchestrator = ProjectOrchestrator(
        tmp_path / "o.db",
        agent_runtime=InProcessAgentRuntime(),
        backend=FakeLLMBackend(default=routing()),
        workspace_root=str(tmp_path / "workspaces"),
    )
    _decision, project = await orchestrator.submit("何かして")

    decision = await orchestrator.grant_resource(project.id, "file:anything")

    assert decision.allowed is False
    assert "no grant policy" in decision.reason
    orchestrator.close()


# --- request -> allow -> the same task continues --------------------------------


async def test_an_agent_can_ask_for_more_and_the_same_task_continues(tmp_path):
    """The loop the whole feature exists for."""
    calls: list[list[dict]] = []

    def asks_then_finishes(config, envelope):
        calls.append(list(config.resources))
        if len(calls) == 1:
            return [
                A2AMessage(
                    type=A2AMessageType.NEED_RESOURCE,
                    project_id=config.project_id,
                    payload={
                        "required_resource": "昨年のSAP実績",
                        "reason": "workspaceに前年比較の材料がない",
                    },
                )
            ]
        return [
            A2AMessage(
                type=A2AMessageType.PROJECT_COMPLETED,
                project_id=config.project_id,
                payload={"summary": "前年比較を作成した"},
            )
        ]

    orchestrator, _shared, _ = build(tmp_path, behaviour=asks_then_finishes)
    _decision, project = await orchestrator.submit("前年比較を作って")

    # The Agent asked, and the Project is waiting on that answer.
    assert orchestrator.projects.get(project.id).status is ProjectStatus.BLOCKED
    [request] = orchestrator.grant_requests(project.id)
    assert request.requested == "昨年のSAP実績"
    assert request.uri is None  # naming the real resource is a person's job

    decision = await orchestrator.grant_resource(
        project.id, "file:sap.csv", reason="前年比較のため"
    )

    assert decision.allowed is True
    # Same project, same agent, now with the resource in its workspace.
    assert [entry["path"] for entry in calls[-1]] == ["resources/sap.csv"]
    assert (
        workspace_of(tmp_path, project) / RESOURCES_DIR / "sap.csv"
    ).read_text(encoding="utf-8").startswith("year,amount")
    assert orchestrator.projects.get(project.id).status is ProjectStatus.COMPLETED
    assert len(orchestrator.projects.all()) == 1
    orchestrator.close()


async def test_a_refused_request_does_not_restart_the_task(tmp_path):
    def always_asks(config, envelope):
        return [
            A2AMessage(
                type=A2AMessageType.NEED_RESOURCE,
                project_id=config.project_id,
                payload={"required_resource": "秘密鍵", "reason": "必要だから"},
            )
        ]

    orchestrator, _shared, private = build(tmp_path, behaviour=always_asks)
    _decision, project = await orchestrator.submit("鍵を使って")

    decision = await orchestrator.grant_resource(
        project.id, f"file:{private / 'keys.txt'}"
    )

    assert decision.allowed is False
    assert orchestrator.projects.get(project.id).status is ProjectStatus.BLOCKED
    orchestrator.close()


async def test_granting_the_same_uri_again_replaces_rather_than_duplicates(tmp_path):
    orchestrator, _shared, _ = build(tmp_path)
    _decision, project = await orchestrator.submit("計画を更新して")

    await orchestrator.grant_resource(project.id, "file:plan.md")
    await orchestrator.grant_resource(
        project.id, "file:plan.md", access=AccessMode.READ_WRITE
    )

    granted = orchestrator.granted(project.id)
    assert [g.uri for g in granted] == ["file:plan.md"]
    assert granted[0].access is AccessMode.READ_WRITE
    orchestrator.close()


# --- writable grants come back ---------------------------------------------------


async def test_a_writable_grant_is_carried_back_when_collected(tmp_path):
    orchestrator, shared, _ = build(tmp_path)
    _decision, project = await orchestrator.submit("計画を更新して")
    await orchestrator.grant_resource(
        project.id, "file:plan.md", access=AccessMode.READ_WRITE
    )

    edited = workspace_of(tmp_path, project) / RESOURCES_DIR / "plan.md"
    edited.write_text("計画v2（Agentが更新）", encoding="utf-8")
    written = orchestrator.collect_workspace(project.id)

    assert written == ["file:plan.md"]
    assert (shared / "plan.md").read_text(encoding="utf-8") == "計画v2（Agentが更新）"
    orchestrator.close()


async def test_a_read_grant_never_comes_back(tmp_path):
    orchestrator, shared, _ = build(tmp_path)
    _decision, project = await orchestrator.submit("仕様を読んで")
    await orchestrator.grant_resource(project.id, "file:spec.md")

    copy = workspace_of(tmp_path, project) / RESOURCES_DIR / "spec.md"
    copy.chmod(0o644)
    copy.write_text("改竄", encoding="utf-8")

    assert orchestrator.collect_workspace(project.id) == []
    assert (shared / "spec.md").read_text(encoding="utf-8") == "共通仕様"
    orchestrator.close()


# --- the A2A wire stays valid A2A -------------------------------------------------


def test_the_manifest_travels_as_a_namespaced_a2a_extension(tmp_path):
    from nexus_seed.orchestrator.models import ProjectAgentConfig
    from nexus_seed.providers.project_agent import _send_params

    config = ProjectAgentConfig(
        agent_id="agent-1",
        project_id="project-1",
        goal="g",
        workspace="/w/project-1",
        resources=[{"path": "resources/spec.md", "access": "read", "uri": "file:spec.md"}],
    )
    params = _send_params({"type": "PROJECT_ASSIGNMENT"}, config)

    metadata = params["metadata"]
    assert metadata[f"{WORKSPACE_EXTENSION}/workspace"] == "/w/project-1"
    assert metadata[f"{WORKSPACE_EXTENSION}/resources"][0]["path"] == "resources/spec.md"
    # Still an ordinary A2A message/send: the extension only adds metadata keys.
    assert params["message"]["kind"] == "message"
    assert params["message"]["parts"][0]["kind"] == "data"


def test_the_assignment_carries_the_manifest_for_an_agent_that_reads_it(tmp_path):
    from nexus_seed.orchestrator.models import ProjectAgentConfig
    from nexus_seed.providers.a2a import A2AEndpoint

    transport = A2AProjectAgentTransport(A2AEndpoint(url="http://127.0.0.1:1"))
    config = ProjectAgentConfig(
        agent_id="agent-1",
        project_id="project-1",
        goal="g",
        workspace="/w/project-1",
        resources=[{"path": "resources/spec.md", "access": "read"}],
    )

    assignment = transport.assignment(config, {"kind": "ASSIGN_GOAL"})

    assert assignment["workspace"] == "/w/project-1"
    assert assignment["resources"] == [{"path": "resources/spec.md", "access": "read"}]


def test_the_instruction_tells_the_agent_how_to_ask_for_more():
    from nexus_seed.providers.project_agent import PROJECT_AGENT_INSTRUCTION

    assert "NEED_RESOURCE" in PROJECT_AGENT_INSTRUCTION
    assert "resources" in PROJECT_AGENT_INSTRUCTION
    assert "read_write" in PROJECT_AGENT_INSTRUCTION
    assert "Work inside the workspace" in PROJECT_AGENT_INSTRUCTION
