"""Phase B: a hand-over to an Agent is durable, retried, and survives a restart.

A Project takes as long as it takes, so NEXUS SEED hands the work over, records
what is outstanding, and comes back to it — including in a process that never
saw the hand-over happen.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AAgentRuntime,
    A2AMessageType,
    AgentStatus,
    AssignmentStatus,
    ProjectOrchestrator,
    ProjectStatus,
)
from tests.test_project_agent_runtime import FakeTransport

COMPLETED = [[(A2AMessageType.PROJECT_COMPLETED, {"summary": "7月は地域Aの単価下落"})]]


def create_decision(goal="売上分析"):
    return proposal_response(
        {"action": "CREATE_PROJECT", "proposed_goal": goal, "reason": "new", "confidence": 0.9}
    )


def orchestrator(db_path, transport, **kwargs):
    return ProjectOrchestrator(
        db_path,
        agent_runtime=A2AAgentRuntime(transport),
        backend=FakeLLMBackend(default=create_decision()),
        retry_base_seconds=0.0,
        **kwargs,
    )


def assignment_of(orch, project_id):
    agent = orch.agents.for_project(project_id)
    return agent.assignment if agent is not None else None


async def test_project_dispatch_is_durable(tmp_path):
    """What the Agent owes is on the Agent record, not only in memory."""
    transport = FakeTransport(COMPLETED, slow=True)
    orch = orchestrator(tmp_path / "o.db", transport)

    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.ACTIVE
    assignment = assignment_of(orch, project.id)
    assert assignment.status is AssignmentStatus.DISPATCHED
    assert assignment.handle == "task-1"
    assert assignment.kind == "ASSIGN_GOAL"
    assert assignment.dispatched_at is not None
    orch.close()

    # Read back by a process that never saw the hand-over.
    reopened = orchestrator(tmp_path / "o.db", FakeTransport())
    restored = assignment_of(reopened, project.id)
    assert restored.handle == "task-1"
    assert restored.status is AssignmentStatus.DISPATCHED
    reopened.close()


async def test_restart_reconciles_active_project(tmp_path):
    """Whatever the Agent did while NEXUS SEED was down is picked up on restart."""
    db_path = tmp_path / "o.db"
    first = FakeTransport(COMPLETED, slow=True)
    orch = orchestrator(db_path, first)
    await orch.handle_request("売上を分析して")
    project_id = orch.projects.all()[0].id
    orch.close()

    # A new process, a new runtime: the answer is waiting under the same handle.
    second = FakeTransport(slow=True)
    second._pending["task-1"] = first._pending["task-1"]
    restarted = orchestrator(db_path, second)

    handled = await restarted.reconcile()

    assert [message.type for message in handled] == [A2AMessageType.PROJECT_COMPLETED]
    project = restarted.projects.get(project_id)
    assert project.status is ProjectStatus.COMPLETED
    assert project.summary == "7月は地域Aの単価下落"
    assert assignment_of(restarted, project_id).status is AssignmentStatus.ANSWERED
    restarted.close()


async def test_restart_reattaches_same_agent(tmp_path):
    """One Project keeps one Agent across a restart — never a second one."""
    db_path = tmp_path / "o.db"
    orch = orchestrator(db_path, FakeTransport(COMPLETED, slow=True))
    await orch.handle_request("売上を分析して")
    project = orch.projects.all()[0]
    agent_id = project.assigned_agent_id
    orch.close()

    second = FakeTransport(slow=True)
    restarted = orchestrator(db_path, second)
    await restarted.reconcile()

    assert [a.agent_id for a in restarted.agent_store.for_project(project.id)] == [agent_id]
    # Re-adopted from the database, not started again.
    assert second.opened == []
    assert second.collected == ["task-1"]
    assert restarted.agent_runtime.configs[agent_id].project_id == project.id
    restarted.close()


async def test_missing_remote_task_can_be_safely_redispatched(tmp_path):
    """An Agent that has forgotten the work is given it again — only then."""
    transport = FakeTransport([[], *COMPLETED], slow=True, lose_work=True)
    orch = orchestrator(tmp_path / "o.db", transport)
    await orch.handle_request("売上を分析して")
    project = orch.projects.all()[0]
    assert len(transport.sent) == 1

    await orch.reconcile()

    # Handed over again, under a new handle, without a second Agent.
    assert len(transport.sent) == 2
    assignment = assignment_of(orch, project.id)
    assert assignment.status is AssignmentStatus.DISPATCHED
    assert assignment.handle == "task-2"
    assert len(orch.agent_store.for_project(project.id)) == 1

    transport.lose_work = False
    await orch.reconcile()
    assert orch.projects.get(project.id).status is ProjectStatus.COMPLETED
    orch.close()


async def test_agent_unavailable_does_not_block_project(tmp_path):
    """A runtime that is down is not a Project that cannot proceed."""
    transport = FakeTransport(COMPLETED, fail_send=True)
    orch = orchestrator(tmp_path / "o.db", transport)

    await orch.handle_request("売上を分析して")

    project = orch.projects.all()[0]
    assert project.status is ProjectStatus.ACTIVE
    assert project.blockers == []
    agent = orch.agents.for_project(project.id)
    assert agent.status is AgentStatus.RUNNING
    assert agent.assignment.status is AssignmentStatus.PENDING

    # And it goes through once the agent is back, with nothing else needed.
    transport.fail_send = False
    await orch.reconcile()

    assert orch.projects.get(project.id).status is ProjectStatus.COMPLETED
    orch.close()


async def test_transport_retry_is_bounded(tmp_path):
    """NEXUS SEED tries a few times and then stops asking — never a busy loop."""
    transport = FakeTransport(fail_send=True)
    orch = orchestrator(tmp_path / "o.db", transport, max_dispatch_attempts=3)

    await orch.handle_request("売上を分析して")
    for _ in range(10):
        await orch.reconcile()

    project = orch.projects.all()[0]
    assignment = assignment_of(orch, project.id)
    assert len(transport.sent) == 3
    assert assignment.attempts == 3
    assert assignment.status is AssignmentStatus.UNAVAILABLE
    assert assignment.next_attempt_at is None
    # Still not the Project's fault, and still recoverable.
    assert project.status is ProjectStatus.ACTIVE
    assert project.blockers == []
    orch.close()


async def test_a_dispatched_hand_over_is_given_up_on_after_its_deadline(tmp_path):
    """An Agent that never answers is abandoned, and the Project is untouched."""
    transport = FakeTransport(COMPLETED, slow=True)
    orch = orchestrator(
        tmp_path / "o.db", transport, assignment_timeout_seconds=0.0
    )
    await orch.handle_request("売上を分析して")
    transport._pending.clear()  # the agent goes quiet

    await orch.reconcile()

    project = orch.projects.all()[0]
    assert transport.abandoned == ["task-1"]
    assert project.status is ProjectStatus.ACTIVE
    assert project.blockers == []
    assert "did not answer" in assignment_of(orch, project.id).error
    orch.close()
