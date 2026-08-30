"""Phase D: a person answers what a Project is stuck on, and it carries on.

The load-bearing case of the whole redesign: a Project blocks on something only
a person can settle, the person says what to do, and *that same Project* with
*that same Agent* continues — rather than a second project being started
alongside the one that is already waiting.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AMessageType,
    AgentStatus,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    RoutingAction,
    completed_message,
    escalation,
)

BLOCKED_REQUEST = "sales.csvを分析し、SAPから前年同期データも取得して比較して"
FOLLOW_UP = "SAPは使わなくていい。今ある2026年6月データだけで分析を続けて"


def create(goal):
    return proposal_response(
        {"action": "CREATE_PROJECT", "proposed_goal": goal, "reason": "new", "confidence": 0.9}
    )


def add_task(project_id, task):
    return proposal_response(
        {
            "action": "ADD_TASK_TO_PROJECT",
            "target_project_id": project_id,
            "proposed_task": task,
            "reason": "answers what the project is waiting on",
            "confidence": 0.9,
        }
    )


def stuck_then_finished(escalate):
    """An Agent that escalates first and finishes once it is told how to go on."""
    calls = {"n": 0}

    def behaviour(config, envelope):
        calls["n"] += 1
        if calls["n"] == 1:
            return [escalate(config.project_id)]
        return [completed_message(config.project_id, "6月データのみで分析を完了した")]

    behaviour.calls = calls
    return behaviour


def orchestrator(tmp_path, behaviour, backend):
    return ProjectOrchestrator(
        tmp_path / "o.db",
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=backend,
    )


async def blocked_project(tmp_path, escalate):
    """Drive one Project into the state a person has to get it out of."""
    behaviour = stuck_then_finished(escalate)
    backend = FakeLLMBackend(default=create(BLOCKED_REQUEST))
    orch = orchestrator(tmp_path, behaviour, backend)
    await orch.handle_request(BLOCKED_REQUEST)
    project = orch.projects.all()[0]
    return orch, backend, project


def needs_resource(project_id):
    return escalation(
        project_id,
        A2AMessageType.NEED_RESOURCE,
        required_resource="sap_prior_year_sales",
        reason="SAPの前年同期データが取得できない",
    )


def needs_human(project_id):
    return escalation(
        project_id,
        A2AMessageType.NEED_HUMAN_INPUT,
        question="分析対象は全地域ですか？",
        reason="対象範囲が決まっていない",
    )


async def test_blocked_project_accepts_followup_instruction(tmp_path):
    orch, backend, project = await blocked_project(tmp_path, needs_resource)
    assert project.status is ProjectStatus.BLOCKED
    backend.default = add_task(project.id, FOLLOW_UP)

    decision = await orch.handle_request(FOLLOW_UP)

    assert decision.action is RoutingAction.ADD_TASK_TO_PROJECT
    assert len(orch.projects.all()) == 1
    assert [task["description"] for task in orch.projects.get(project.id).tasks] == [FOLLOW_UP]
    orch.close()


async def test_followup_routes_to_existing_project(tmp_path):
    """A blocked Project is still offered to the router, or the answer is lost."""
    orch, _backend, project = await blocked_project(tmp_path, needs_resource)

    context = orch.context.build(FOLLOW_UP)

    [offered] = context.active_projects
    assert offered["id"] == project.id
    assert offered["status"] == "BLOCKED"
    assert offered["blockers"] == ["SAPの前年同期データが取得できない"]
    orch.close()


async def test_followup_reuses_same_agent(tmp_path):
    orch, backend, project = await blocked_project(tmp_path, needs_resource)
    agent_id = project.assigned_agent_id
    backend.default = add_task(project.id, FOLLOW_UP)

    await orch.handle_request(FOLLOW_UP)

    assert [a.agent_id for a in orch.agent_store.for_project(project.id)] == [agent_id]
    agent = orch.agents.for_project(project.id)
    assert agent.status is AgentStatus.IDLE  # idled again by its own completion
    # The follow-up went to that Agent, with what it was stuck on attached.
    _, envelope = orch.agent_runtime.delivered[-1]
    assert envelope["kind"] == "ADD_TASK"
    assert envelope["task"]["description"] == FOLLOW_UP
    assert envelope["blockers"] == [
        {
            "kind": "NEED_RESOURCE",
            "reason": "SAPの前年同期データが取得できない",
            "resolved": True,
        }
    ]
    orch.close()


async def test_followup_clears_current_blocker(tmp_path):
    orch, backend, project = await blocked_project(tmp_path, needs_resource)
    backend.default = add_task(project.id, FOLLOW_UP)

    await orch.handle_request(FOLLOW_UP)

    resumed = orch.projects.get(project.id)
    assert resumed.current_blockers == []
    # The history stays, and says what resolved it.
    [blocker] = resumed.blockers
    assert blocker["kind"] == "NEED_RESOURCE"
    assert blocker["resolved_at"]
    assert blocker["resolved_by"].startswith("task ")
    orch.close()


async def test_waiting_human_project_resumes_after_answer(tmp_path):
    orch, backend, project = await blocked_project(tmp_path, needs_human)
    assert project.status is ProjectStatus.WAITING_HUMAN
    backend.default = add_task(project.id, "国内地域だけでいい")

    await orch.handle_request("国内地域だけでいい")

    resumed = orch.projects.get(project.id)
    assert resumed.status is ProjectStatus.COMPLETED
    assert resumed.current_blockers == []
    orch.close()


async def test_resumed_project_can_complete(tmp_path):
    orch, backend, project = await blocked_project(tmp_path, needs_resource)
    backend.default = add_task(project.id, FOLLOW_UP)

    await orch.handle_request(FOLLOW_UP)

    resumed = orch.projects.get(project.id)
    assert resumed.status is ProjectStatus.COMPLETED
    assert resumed.summary == "6月データのみで分析を完了した"
    agent = orch.agents.for_project(project.id)
    assert agent.status is AgentStatus.IDLE

    # The whole story is on the audited channel, in order.
    inbound = [m.type for d, m in orch.gateway.history(project.id) if d == "inbound"]
    assert inbound == [A2AMessageType.NEED_RESOURCE, A2AMessageType.PROJECT_COMPLETED]
    orch.close()
