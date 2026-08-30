"""Case 4: an Agent discovers an independent problem; NEXUS SEED owns creation."""

from __future__ import annotations

from nexus_seed.backends import BackendResult, FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AMessage,
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
)


class ScriptedRouterBackend:
    """Answers each routing call from a queue, so discovery can differ."""

    def __init__(self, results: list[BackendResult]) -> None:
        self.results = list(results)
        self.calls = []

    async def execute(self, request):
        self.calls.append(request)
        return self.results.pop(0) if self.results else self.results[-1]


def decision(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


async def test_case4_discovery_creates_child_project_with_new_agent(tmp_path):
    discovered = "入力データの品質問題を是正する"

    def behaviour(config, envelope):
        if envelope["kind"] != "ASSIGN_GOAL" or config.goal == discovered:
            return []
        return [
            A2AMessage(
                type=A2AMessageType.DISCOVERED_NEW_PROJECT,
                project_id=config.project_id,
                payload={"goal": discovered, "priority": 5},
            )
        ]

    backend = ScriptedRouterBackend(
        [
            decision("CREATE_PROJECT", proposed_goal="売上低下原因を調査する"),
            decision("CREATE_PROJECT", proposed_goal=discovered),
        ]
    )
    runtime = InProcessAgentRuntime(behaviour=behaviour)
    orch = ProjectOrchestrator(tmp_path / "o.db", agent_runtime=runtime, backend=backend)

    await orch.handle_request("売上低下原因を調べて")

    projects = orch.projects.all()
    assert len(projects) == 2
    parent = [p for p in projects if p.parent_project_id is None][0]
    child = [p for p in projects if p.parent_project_id is not None][0]

    # The Agent reported; NEXUS SEED created the project.
    assert child.goal == discovered
    assert child.parent_project_id == parent.id
    assert child.priority == 5
    assert child.status is ProjectStatus.ACTIVE

    # A separate Agent owns the child project (1 project = 1 agent).
    assert child.assigned_agent_id is not None
    assert child.assigned_agent_id != parent.assigned_agent_id
    assert orch.projects.children_of(parent.id) == [child] or [
        p.id for p in orch.projects.children_of(parent.id)
    ] == [child.id]

    # Routing was consulted for the discovery, not bypassed.
    assert len(backend.calls) == 2
    assert backend.calls[1].context["origin_project_id"] == parent.id
    assert backend.calls[1].context["source"] == "agent_discovery"
    orch.close()


async def test_discovery_without_goal_is_ignored(tmp_path):
    def behaviour(config, envelope):
        return [
            A2AMessage(
                type=A2AMessageType.DISCOVERED_NEW_PROJECT,
                project_id=config.project_id,
                payload={},
            )
        ]

    backend = FakeLLMBackend(default=decision("CREATE_PROJECT", proposed_goal="g"))
    orch = ProjectOrchestrator(
        tmp_path / "o.db",
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=backend,
    )

    await orch.handle_request("やって")

    assert len(orch.projects.all()) == 1  # nothing was created from an empty discovery
    orch.close()
