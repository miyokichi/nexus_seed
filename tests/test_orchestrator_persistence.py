"""Orchestrator state survives a restart, rebuilt from SQLite alone."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    completed_message,
    escalation,
)


def create_decision(goal="売上分析"):
    return proposal_response(
        {"action": "CREATE_PROJECT", "proposed_goal": goal, "reason": "n", "confidence": 0.9}
    )


async def test_blocked_project_and_agent_survive_restart(tmp_path):
    db_path = tmp_path / "o.db"

    def blocking(config, envelope):
        return [
            escalation(
                config.project_id,
                A2AMessageType.NEED_CAPABILITY,
                required_capability="sap",
                reason="needs SAP",
            )
        ]

    orch = ProjectOrchestrator(
        db_path,
        agent_runtime=InProcessAgentRuntime(behaviour=blocking),
        backend=FakeLLMBackend(default=create_decision()),
    )
    await orch.handle_request("売上を分析して")
    project = orch.projects.all()[0]
    project_id, agent_id = project.id, project.assigned_agent_id
    assert project.status is ProjectStatus.BLOCKED
    orch.close()

    # --- rebuilt from the same database, nothing carried in memory ---
    def finishing(config, envelope):
        return [completed_message(config.project_id, "done")]

    orch2 = ProjectOrchestrator(
        db_path,
        agent_runtime=InProcessAgentRuntime(behaviour=finishing),
        backend=FakeLLMBackend(default=create_decision()),
    )
    recovered = orch2.projects.get(project_id)
    assert recovered is not None
    assert recovered.status is ProjectStatus.BLOCKED
    assert recovered.assigned_agent_id == agent_id
    assert recovered.blockers[0]["detail"]["required_capability"] == "sap"

    # The audited A2A history is still there.
    inbound = [m for d, m in orch2.gateway.history(project_id) if d == "inbound"]
    assert [m.type for m in inbound] == [A2AMessageType.NEED_CAPABILITY]

    # A new runtime reuses the recorded agent rather than spawning a second one.
    agent = orch2.agents.for_project(project_id)
    assert agent is not None and agent.agent_id == agent_id
    orch2.close()


async def test_project_record_roundtrips_every_field(tmp_path):
    orch = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=InProcessAgentRuntime())
    project = orch.projects.create(
        "goal text", context={"k": "v"}, priority=7, parent_project_id="project-parent"
    )
    orch.projects.add_task(project, "task one")
    orch.projects.add_blocker(project, kind="NEED_RESOURCE", reason="no data")
    orch.projects.set_summary(project, "half done")
    orch.projects.assign_agent(project, "agent-x")
    orch.close()

    orch2 = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=InProcessAgentRuntime())
    restored = orch2.projects.get(project.id)
    assert restored.goal == "goal text"
    assert restored.context == {"k": "v"}
    assert restored.priority == 7
    assert restored.parent_project_id == "project-parent"
    assert restored.summary == "half done"
    assert restored.assigned_agent_id == "agent-x"
    assert [t["description"] for t in restored.tasks] == ["task one"]
    assert restored.blockers[0]["kind"] == "NEED_RESOURCE"
    assert restored.to_dict()["status"] == "CREATED"
    orch2.close()


async def test_live_projects_are_ordered_by_priority(tmp_path):
    orch = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=InProcessAgentRuntime())
    orch.projects.create("low", priority=1)
    high = orch.projects.create("high", priority=9)
    done = orch.projects.create("done")
    orch.projects.complete(done)

    live = orch.projects.live()

    assert [p.goal for p in live] == ["high", "low"]
    assert high.id == live[0].id
    orch.close()
