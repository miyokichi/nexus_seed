"""Cases 1 and 2: a new Goal becomes a Project; a follow-up joins an existing one."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    RoutingAction,
)


def decision(action, **fields):
    payload = {"action": action, "reason": "test", "confidence": 0.9, **fields}
    return proposal_response(payload)


async def test_case1_new_goal_creates_project_and_agent(tmp_path):
    backend = FakeLLMBackend(
        script=[decision("CREATE_PROJECT", proposed_goal="7月の売上低下原因を調べる")]
    )
    runtime = InProcessAgentRuntime()
    orch = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=runtime, backend=backend)

    result = await orch.handle_request("7月の売上低下原因を調べて")

    assert result.action is RoutingAction.CREATE_PROJECT
    projects = orch.projects.all()
    assert len(projects) == 1
    project = projects[0]
    assert project.goal == "7月の売上低下原因を調べる"
    # An Agent was assigned and the whole Goal delegated to it.
    assert project.assigned_agent_id is not None
    assert project.status is ProjectStatus.ACTIVE
    agent = orch.agents.for_project(project.id)
    assert agent is not None and agent.agent_id == project.assigned_agent_id

    delivered = [envelope for _, envelope in runtime.delivered]
    assert delivered == [
        {
            "kind": "ASSIGN_GOAL",
            "project_id": project.id,
            "goal": project.goal,
            "context": project.context,
            "priority": 0,
        }
    ]
    orch.close()


async def test_case2_followup_joins_existing_project(tmp_path):
    runtime = InProcessAgentRuntime()
    backend = FakeLLMBackend()
    orch = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=runtime, backend=backend)

    backend.script = [decision("CREATE_PROJECT", proposed_goal="7月の売上低下原因を調べる")]
    await orch.handle_request("7月の売上低下原因を調べて")
    project = orch.projects.all()[0]
    agent_id = project.assigned_agent_id

    # "地域別でも見て" is more work for the same goal, not a new project.
    backend.script = [
        decision(
            "CREATE_PROJECT", proposed_goal="7月の売上低下原因を調べる"
        ),
        decision(
            "ADD_TASK_TO_PROJECT",
            target_project_id=project.id,
            proposed_task="地域別でも分析する",
        ),
    ]
    result = await orch.handle_request("地域別でも見て")

    assert result.action is RoutingAction.ADD_TASK_TO_PROJECT
    # No second project, and the same Agent keeps the work.
    assert len(orch.projects.all()) == 1
    updated = orch.projects.get(project.id)
    assert [task["description"] for task in updated.tasks] == ["地域別でも分析する"]
    assert updated.assigned_agent_id == agent_id

    kinds = [envelope["kind"] for _, envelope in runtime.delivered]
    assert kinds == ["ASSIGN_GOAL", "ADD_TASK"]
    orch.close()


async def test_unknown_target_project_falls_back_to_new_project(tmp_path):
    backend = FakeLLMBackend(
        default=decision("ADD_TASK_TO_PROJECT", target_project_id="project-does-not-exist")
    )
    orch = ProjectOrchestrator(
        tmp_path / "o.db", agent_runtime=InProcessAgentRuntime(), backend=backend
    )

    result = await orch.handle_request("何かして")

    # A decision naming a project that does not exist is not acted on as-is.
    assert result.action is RoutingAction.CREATE_PROJECT
    assert "unknown project" in result.reason
    assert len(orch.projects.all()) == 1
    orch.close()


async def test_router_without_backend_creates_project(tmp_path):
    orch = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=InProcessAgentRuntime())

    result = await orch.handle_request("調べておいて")

    assert result.action is RoutingAction.CREATE_PROJECT
    assert result.confidence == 0.0
    assert orch.projects.all()[0].goal == "調べておいて"
    orch.close()
