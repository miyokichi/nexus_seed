"""Cases 3 and 5: an Agent escalates and blocks; an Agent completes its Project."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AMessageType,
    AgentStatus,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    completed_message,
    escalation,
    status_message,
)


def create_decision(goal="売上分析"):
    return proposal_response(
        {"action": "CREATE_PROJECT", "proposed_goal": goal, "reason": "new", "confidence": 0.9}
    )


def orchestrator(tmp_path, behaviour):
    runtime = InProcessAgentRuntime(behaviour=behaviour)
    backend = FakeLLMBackend(default=create_decision())
    return ProjectOrchestrator(tmp_path / "o.db", agent_runtime=runtime, backend=backend), runtime


async def test_case3_need_capability_blocks_project(tmp_path):
    def behaviour(config, envelope):
        return [
            escalation(
                config.project_id,
                A2AMessageType.NEED_CAPABILITY,
                required_capability="sap_sales_data_access",
                reason="Transaction-level data is required",
            )
        ]

    orch, _ = orchestrator(tmp_path, behaviour)
    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.BLOCKED
    assert len(project.blockers) == 1
    blocker = project.blockers[0]
    assert blocker["kind"] == "NEED_CAPABILITY"
    assert blocker["detail"]["required_capability"] == "sap_sales_data_access"

    # The escalation is recorded on the audited A2A channel.
    inbound = [m for direction, m in orch.gateway.history(project.id) if direction == "inbound"]
    assert [m.type for m in inbound] == [A2AMessageType.NEED_CAPABILITY]
    orch.close()


async def test_need_human_input_waits_for_human(tmp_path):
    def behaviour(config, envelope):
        return [
            escalation(
                config.project_id,
                A2AMessageType.NEED_HUMAN_INPUT,
                reason="Which region should be prioritised?",
            )
        ]

    orch, _ = orchestrator(tmp_path, behaviour)
    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.WAITING_HUMAN
    assert project.blockers[0]["reason"] == "Which region should be prioritised?"
    orch.close()


async def test_case5_completion_completes_project_and_idles_agent(tmp_path):
    def behaviour(config, envelope):
        return [
            status_message(config.project_id, "CSVを取得した"),
            completed_message(config.project_id, "原因は地域Aの単価下落"),
        ]

    orch, _ = orchestrator(tmp_path, behaviour)
    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.COMPLETED
    assert project.summary == "原因は地域Aの単価下落"

    agent = orch.agent_store.for_project(project.id)[0]
    assert agent.status is AgentStatus.IDLE
    orch.close()


async def test_blocked_project_can_be_resolved_and_resumed(tmp_path):
    calls = {"n": 0}

    def behaviour(config, envelope):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                escalation(
                    config.project_id,
                    A2AMessageType.NEED_CAPABILITY,
                    reason="needs SAP access",
                )
            ]
        return [completed_message(config.project_id, "done with access")]

    orch, _ = orchestrator(tmp_path, behaviour)
    await orch.handle_request("売上を分析して")
    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.BLOCKED

    resolved = await orch.resolve_block(project.id, note="SAP access granted")

    assert resolved.status is ProjectStatus.COMPLETED
    assert resolved.blockers == []
    orch.close()


async def test_message_for_unknown_project_is_ignored(tmp_path):
    orch, runtime = orchestrator(tmp_path, None)
    runtime.emit(escalation("project-nope", A2AMessageType.PROJECT_BLOCKED, reason="x"))

    handled = await orch.drain()

    assert [m.type for m in handled] == [A2AMessageType.PROJECT_BLOCKED]
    assert orch.projects.all() == []
    orch.close()
