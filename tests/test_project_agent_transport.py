"""Handing a whole Project to an external Agent over the real A2A wire.

A real HTTP server speaking JSON-RPC, as in ``test_a2a_provider.py``: these
tests exist to prove the *protocol* boundary, so no particular agent product is
a test dependency and no live agent has to be running.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AAgentRuntime,
    A2AMessageType,
    AgentStatus,
    AgentUnavailable,
    AssignmentStatus,
    ProjectAgentConfig,
    ProjectOrchestrator,
    ProjectStatus,
)
from nexus_seed.providers.a2a import A2AEndpoint
from nexus_seed.providers.project_agent import (
    PROJECT_ASSIGNMENT,
    REPLY_SCHEMA,
    A2AProjectAgentTransport,
)
from tests.a2a_helpers import FakeA2AServer, data_artifact, free_url, task, text_artifact


def answer(*messages) -> dict:
    """One finished remote task carrying what the Project Agent reported."""
    return task(
        "completed",
        artifacts=[
            data_artifact(
                {
                    "messages": [
                        {"type": message_type.value, "payload": payload}
                        for message_type, payload in messages
                    ]
                }
            )
        ],
    )


def endpoint(url: str) -> A2AEndpoint:
    return A2AEndpoint(url=url, poll_interval_seconds=0.01, timeout_seconds=5.0)


def transport(url: str) -> A2AProjectAgentTransport:
    return A2AProjectAgentTransport(endpoint(url))


def orchestrator(db_path, url, **kwargs):
    return ProjectOrchestrator(
        db_path,
        agent_runtime=A2AAgentRuntime(transport(url)),
        backend=FakeLLMBackend(
            default=proposal_response(
                {
                    "action": "CREATE_PROJECT",
                    "proposed_goal": "7月の売上低下原因を特定する",
                    "reason": "new",
                    "confidence": 0.9,
                }
            )
        ),
        **kwargs,
    )


def config(**overrides) -> ProjectAgentConfig:
    values = {
        "agent_id": "agent-1",
        "project_id": "project-1",
        "goal": "7月の売上低下原因を特定する",
        "project_context": {"csv": "samples/sample_sales.csv"},
        "constraints": {"baseline_month": "2026-06"},
        "workspace": "projects/project-1",
    }
    values.update(overrides)
    return ProjectAgentConfig(**values)


async def test_assignment_carries_the_goal_and_nothing_about_method():
    sent = transport("http://127.0.0.1:1").assignment(
        config(), {"kind": "ASSIGN_GOAL"}
    )

    assert sent["type"] == PROJECT_ASSIGNMENT
    assert sent["goal"] == "7月の売上低下原因を特定する"
    assert sent["context"] == {"csv": "samples/sample_sales.csv"}
    assert sent["constraints"] == {"baseline_month": "2026-06"}
    assert sent["workspace"] == "projects/project-1"
    assert "task" not in sent

    # A Project is delegated as a goal, not as a method: what the Agent can do
    # is its own configuration, and NEXUS SEED does not send it.
    assert "available_skills" not in sent
    assert "skills" not in sent


async def test_added_task_reaches_the_agent_that_already_owns_the_goal():
    sent = transport("http://127.0.0.1:1").assignment(
        config(), {"kind": "ADD_TASK", "task": {"id": "t-1", "description": "地域別も見て"}}
    )

    assert sent["goal"] == "7月の売上低下原因を特定する"
    assert sent["task"] == {"id": "t-1", "description": "地域別も見て"}


async def test_project_completed_over_a2a_completes_the_project(tmp_path):
    with FakeA2AServer(
        {
            "message/send": [
                answer(
                    (A2AMessageType.PROJECT_STATUS, {"summary": "6月と7月を比較した"}),
                    (
                        A2AMessageType.PROJECT_COMPLETED,
                        {"summary": "7月の低下は地域Aの単価下落が主因"},
                    ),
                )
            ]
        }
    ) as server:
        orch = orchestrator(tmp_path / "o.db", server.url)
        await orch.handle_request("samples/sample_sales.csvを分析して")

        project = orch.projects.all()[0]
        assert project.status is ProjectStatus.COMPLETED
        assert project.summary == "7月の低下は地域Aの単価下落が主因"
        agent = orch.agent_store.for_project(project.id)[0]
        assert agent.status is AgentStatus.IDLE

        # The request really was one A2A message/send carrying the assignment.
        [call] = server.calls("message/send")
        data = call["params"]["message"]["parts"][0]["data"]
        assert data["output_schema"] == REPLY_SCHEMA
        assert data["context"]["assignment"]["project_id"] == project.id
        assert call["params"]["metadata"]["nexus_seed/project_id"] == project.id

        history = orch.gateway.history(project.id)
        assert [m.type for direction, m in history if direction == "inbound"] == [
            A2AMessageType.PROJECT_STATUS,
            A2AMessageType.PROJECT_COMPLETED,
        ]
        orch.close()


async def test_need_capability_over_a2a_blocks_the_project(tmp_path):
    with FakeA2AServer(
        {
            "message/send": [
                answer(
                    (
                        A2AMessageType.NEED_CAPABILITY,
                        {
                            "required_capability": "sap_sales_data_access",
                            "reason": "前年同期データの取得にSAP accessが必要",
                        },
                    )
                )
            ]
        }
    ) as server:
        orch = orchestrator(tmp_path / "o.db", server.url)
        await orch.handle_request("SAPの前年同期と比較して")

        project = orch.projects.all()[0]
        assert project.status is ProjectStatus.BLOCKED
        blocker = project.blockers[0]
        assert blocker["kind"] == "NEED_CAPABILITY"
        assert blocker["detail"]["required_capability"] == "sap_sales_data_access"
        orch.close()


async def test_a_slow_task_is_taken_now_and_answered_later(tmp_path):
    """A Project that takes a while is *accepted* now and finished later."""
    with FakeA2AServer(
        {
            "message/send": [task("working", task_id="task-7")],
            "tasks/get": [
                task("working", task_id="task-7"),
                answer((A2AMessageType.PROJECT_COMPLETED, {"summary": "done"})),
            ],
        }
    ) as server:
        orch = orchestrator(tmp_path / "o.db", server.url)
        await orch.handle_request("時間のかかる分析をして")

        # Accepted, not finished: the request came back while the Agent works.
        project = orch.projects.all()[0]
        assert project.status is ProjectStatus.ACTIVE
        assignment = orch.agents.for_project(project.id).assignment
        assert assignment.status is AssignmentStatus.DISPATCHED
        assert assignment.handle == "task-7"
        assert server.calls("tasks/get") == []

        await orch.reconcile()
        assert orch.projects.all()[0].status is ProjectStatus.ACTIVE

        await orch.reconcile()
        assert orch.projects.all()[0].status is ProjectStatus.COMPLETED
        assert len(server.calls("tasks/get")) == 2
        orch.close()


async def test_an_agent_that_is_not_running_is_unavailable(tmp_path):
    orch = orchestrator(tmp_path / "o.db", free_url())

    await orch.handle_request("売上を分析して")

    # No agent, no blocker, no failure: the project simply has not started.
    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.CREATED
    assert project.blockers == []
    assert orch.agents.for_project(project.id) is None
    orch.close()


async def test_a_failed_remote_task_is_unavailable_not_a_blocker(tmp_path):
    with FakeA2AServer(
        {"message/send": [task("failed", status_message="the model provider timed out")]}
    ) as server:
        orch = orchestrator(tmp_path / "o.db", server.url)
        await orch.handle_request("売上を分析して")

        project = orch.projects.all()[0]
        assert project.blockers == []
        assert project.status is not ProjectStatus.BLOCKED
        agent = orch.agents.for_project(project.id)
        assert "the model provider timed out" in agent.metadata["unavailable"]["reason"]
        orch.close()


async def test_an_answer_outside_the_contract_is_refused_not_guessed():
    with FakeA2AServer(
        {"message/send": [task("completed", artifacts=[text_artifact("I think it went fine")])]}
    ) as server:
        with pytest.raises(AgentUnavailable, match="understand"):
            await transport(server.url).start(config(), {"kind": "ASSIGN_GOAL"})


async def test_an_unknown_message_type_is_dropped_not_invented():
    with FakeA2AServer(
        {
            "message/send": [
                task(
                    "completed",
                    artifacts=[
                        data_artifact(
                            {
                                "messages": [
                                    {"type": "PROJECT_ESCALATED", "payload": {}},
                                    {
                                        "type": "PROJECT_COMPLETED",
                                        "payload": {"summary": "done"},
                                    },
                                ]
                            }
                        )
                    ],
                )
            ]
        }
    ) as server:
        dispatch = await transport(server.url).start(config(), {"kind": "ASSIGN_GOAL"})
        messages = dispatch.messages

        assert [message.type for message in messages] == [A2AMessageType.PROJECT_COMPLETED]
        assert messages[0].project_id == "project-1"
        assert messages[0].source_agent_id == "agent-1"
