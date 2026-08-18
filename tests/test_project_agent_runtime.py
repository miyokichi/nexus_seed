"""A2AAgentRuntime: an external Project Agent, without needing one running.

The transport is faked here so the *orchestration* is what is under test —
exactly as ``InProcessAgentRuntime`` fakes the agent and ``FakeLLMBackend``
fakes the LLM.  ``tests/test_project_agent_transport.py`` covers the real HTTP
boundary, and ``tests/integration/`` covers a real external agent.
"""

from __future__ import annotations

import pytest

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AAgentRuntime,
    A2AMessage,
    A2AMessageType,
    AgentStatus,
    AgentUnavailable,
    AssignmentStatus,
    Dispatch,
    ProjectOrchestrator,
    ProjectStatus,
    RemoteWorkLost,
)


class FakeTransport:
    """A Project Agent runtime that answers with a scripted set of messages.

    ``answers`` is one list of ``(type, payload)`` per hand-over.  By default an
    answer is ready at once; ``slow`` holds it back so the hand-over stays
    outstanding until ``reconcile`` asks for it, the way a real Agent does.
    """

    def __init__(
        self,
        answers=None,
        *,
        fail_open=False,
        fail_send=False,
        slow=False,
        lose_work=False,
    ):
        self.answers = list(answers or [])
        self.fail_open = fail_open
        self.fail_send = fail_send
        self.slow = slow
        self.lose_work = lose_work
        self.opened = []
        self.sent = []
        self.collected = []
        self.abandoned = []
        self._pending = {}

    async def open(self, config):
        if self.fail_open:
            raise AgentUnavailable("agent card unavailable at http://127.0.0.1:8801")
        self.opened.append(config)
        return "http://127.0.0.1:8801"

    async def start(self, config, envelope):
        self.sent.append((config, envelope))
        if self.fail_send:
            raise AgentUnavailable("project agent unreachable: connection refused")
        messages = self._messages(config)
        if not self.slow:
            return Dispatch(messages=messages)
        handle = f"task-{len(self.sent)}"
        self._pending[handle] = messages
        return Dispatch(handle=handle)

    async def collect(self, config, handle):
        self.collected.append(handle)
        if self.lose_work:
            raise RemoteWorkLost(f"project agent no longer knows task {handle}")
        return self._pending.pop(handle, None)

    async def abandon(self, handle):
        self.abandoned.append(handle)
        return True

    async def close(self, agent_id):
        return None

    async def alive(self):
        return not self.fail_open

    def _messages(self, config):
        answers = self.answers.pop(0) if self.answers else []
        return [
            A2AMessage(
                type=message_type,
                project_id=config.project_id,
                source_agent_id=config.agent_id,
                payload=dict(payload),
            )
            for message_type, payload in answers
        ]


def create_decision(goal="売上分析"):
    return proposal_response(
        {"action": "CREATE_PROJECT", "proposed_goal": goal, "reason": "new", "confidence": 0.9}
    )


def orchestrator(db_path, transport, **kwargs):
    return ProjectOrchestrator(
        db_path,
        agent_runtime=A2AAgentRuntime(transport),
        backend=FakeLLMBackend(default=create_decision()),
        available_skills=("load_and_clean_csv", "compute_sales_metrics"),
        **kwargs,
    )


async def test_a2a_runtime_assigns_project(tmp_path):
    transport = FakeTransport(
        [[(A2AMessageType.PROJECT_COMPLETED, {"summary": "7月は地域Aの単価下落"})]]
    )
    orch = orchestrator(tmp_path / "o.db", transport, workspace_root="projects")

    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.COMPLETED
    assert project.summary == "7月は地域Aの単価下落"

    # The whole Goal went out once, with the config the Agent was started with.
    assert len(transport.opened) == 1
    config, envelope = transport.sent[0]
    assert envelope["kind"] == "ASSIGN_GOAL"
    assert envelope["goal"] == project.goal
    assert config.project_id == project.id
    assert config.workspace == f"projects/{project.id}"
    assert config.available_skills == ("load_and_clean_csv", "compute_sales_metrics")

    agent = orch.agent_store.for_project(project.id)[0]
    assert agent.runtime == "a2a"
    assert agent.endpoint == "http://127.0.0.1:8801"
    assert agent.status is AgentStatus.IDLE

    # Both directions of the delegation are on the audited channel.
    inbound = [m for direction, m in orch.gateway.history(project.id) if direction == "inbound"]
    assert [m.type for m in inbound] == [A2AMessageType.PROJECT_COMPLETED]
    assert inbound[0].source_agent_id == agent.agent_id
    orch.close()


async def test_agent_status_updates_project_summary(tmp_path):
    transport = FakeTransport(
        [
            [
                (A2AMessageType.PROJECT_STATUS, {"summary": "6月と7月を比較した"}),
                (A2AMessageType.PROJECT_STATUS, {"summary": "地域別の内訳を調べている"}),
            ]
        ]
    )
    orch = orchestrator(tmp_path / "o.db", transport)

    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    # Progress is a summary, not a task list: NEXUS SEED keeps the latest word
    # on the project and nothing about how the Agent is getting there.
    assert project.summary == "地域別の内訳を調べている"
    assert project.tasks == []
    assert project.status is ProjectStatus.ACTIVE
    orch.close()


async def test_need_capability_from_external_agent_blocks_project(tmp_path):
    transport = FakeTransport(
        [
            [
                (
                    A2AMessageType.NEED_CAPABILITY,
                    {
                        "required_capability": "sap_sales_data_access",
                        "reason": "前年同期データの取得にSAP accessが必要",
                    },
                )
            ]
        ]
    )
    orch = orchestrator(tmp_path / "o.db", transport)

    await orch.handle_request("SAPの前年同期と比較して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.BLOCKED
    assert project.blockers[0]["detail"]["required_capability"] == "sap_sales_data_access"
    orch.close()


async def test_transport_failure_is_not_capability_failure(tmp_path):
    """An agent process that is down must never look like a missing Capability."""
    transport = FakeTransport(fail_send=True)
    orch = orchestrator(tmp_path / "o.db", transport)

    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is not ProjectStatus.BLOCKED
    assert project.status is not ProjectStatus.FAILED
    assert project.blockers == []

    # The project keeps its Agent and the hand-over is queued to be retried.
    agent = orch.agents.for_project(project.id)
    assert agent is not None
    assert agent.status is AgentStatus.RUNNING
    assert "connection refused" in agent.metadata["unavailable"]["reason"]
    assert agent.assignment.status is AssignmentStatus.PENDING
    assert agent.assignment.next_attempt_at is not None
    orch.close()


async def test_unreachable_runtime_leaves_project_without_an_agent(tmp_path):
    orch = orchestrator(tmp_path / "o.db", FakeTransport(fail_open=True))

    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.CREATED
    assert project.blockers == []
    assert orch.agents.for_project(project.id) is None
    failed = orch.agent_store.for_project(project.id)[0]
    assert failed.status is AgentStatus.FAILED
    assert "agent card unavailable" in failed.metadata["error"]
    orch.close()


async def test_restart_restores_external_agent_assignment(tmp_path):
    db_path = tmp_path / "o.db"
    first = FakeTransport([[(A2AMessageType.PROJECT_STATUS, {"summary": "調査中"})]])
    orch = orchestrator(db_path, first)
    await orch.handle_request("売上を分析して")
    project = orch.projects.all()[0]
    project_id, agent_id = project.id, project.assigned_agent_id
    orch.close()

    # --- rebuilt from the database, with a runtime that has never seen it ---
    second = FakeTransport([[(A2AMessageType.PROJECT_COMPLETED, {"summary": "完了"})]])
    orch2 = orchestrator(db_path, second)
    recovered = orch2.projects.get(project_id)
    assert recovered.assigned_agent_id == agent_id
    assert recovered.summary == "調査中"

    resumed = await orch2.resolve_block(project_id, reactivate=True)

    # The same Agent was re-adopted rather than a second one being started.
    assert resumed.status is ProjectStatus.COMPLETED
    assert [agent.agent_id for agent in orch2.agent_store.for_project(project_id)] == [agent_id]
    assert second.opened == []
    assert second.sent[0][0].agent_id == agent_id
    orch2.close()


async def test_delivering_to_an_unknown_agent_is_reported_not_guessed(tmp_path):
    runtime = A2AAgentRuntime(FakeTransport())

    with pytest.raises(AgentUnavailable):
        await runtime.deliver("agent-never-seen", {"kind": "ASSIGN_GOAL"})
