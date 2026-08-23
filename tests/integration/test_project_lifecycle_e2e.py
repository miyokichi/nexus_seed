"""End-to-end: the whole Project lifecycle against a real external Agent.

Skipped unless an A2A Project Agent is pointed at, exactly like
``test_project_agent_e2e``::

    little-agent --serve-a2a --agent analysis_worker --port 8801 --auto-approve
    $env:NEXUS_SEED_PROJECT_AGENT_URL = "http://127.0.0.1:8801"
    pytest tests/integration

This one drives the *durable* path rather than a single delegation: the work is
handed over, NEXUS SEED is closed while the Agent is still working, and a new
process reconciles what happened in between.

Routing is scripted here (``FakeLLMBackend``) so the assertions are about the
lifecycle rather than about a model's routing judgement; the agent doing the
work is real.
"""

from __future__ import annotations

import os

import pytest

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.orchestrator import (
    A2AMessageType,
    AgentStatus,
    AssignmentStatus,
    ProjectStatus,
)
from nexus_seed.orchestrator_config import ProjectAgentSettings, build_orchestrator

AGENT_URL = os.environ.get("NEXUS_SEED_PROJECT_AGENT_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not AGENT_URL,
        reason="set NEXUS_SEED_PROJECT_AGENT_URL to a running A2A Project Agent",
    ),
]

ANALYSIS_REQUEST = (
    "samples/sample_sales.csv を分析し、2026年7月の売上低下原因を特定してください。"
    "2026年6月を比較基準にしてください。"
)

SAP_REQUEST = (
    "samples/sample_sales.csv を分析し、さらにSAPから前年同期データを取得して"
    "比較してください。"
)

FOLLOW_UP = (
    "SAPは使わなくてかまいません。手元の samples/sample_sales.csv だけで、"
    "2026年6月と7月を比較して分析を続けてください。"
)

TERMINAL_ESCALATIONS = {
    A2AMessageType.NEED_CAPABILITY,
    A2AMessageType.NEED_RESOURCE,
    A2AMessageType.NEED_PERMISSION,
    A2AMessageType.PROJECT_BLOCKED,
    A2AMessageType.NEED_HUMAN_INPUT,
}


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


def orchestrator(db_path, backend):
    return build_orchestrator(
        db_path,
        settings=ProjectAgentSettings(runtime="a2a", url=AGENT_URL),
        backend=backend,
    )


async def test_real_agent_project_survives_a_restart_mid_run(tmp_path):
    """NEXUS SEED goes down while the Agent works, and picks the answer up after."""
    db_path = tmp_path / "o.db"
    backend = FakeLLMBackend(default=create(ANALYSIS_REQUEST))
    orch = orchestrator(db_path, backend)
    try:
        _decision, project = await orch.submit(ANALYSIS_REQUEST)
        assert project is not None
        project_id, agent_id = project.id, project.assigned_agent_id

        # Handed over and accepted, with the Agent still working on it.
        assignment = orch.agents.for_project(project_id).assignment
        assert assignment.status is AssignmentStatus.DISPATCHED
        assert assignment.handle
        handle = assignment.handle
        assert project.status is ProjectStatus.ACTIVE
    finally:
        orch.close()

    # --- a new process, which has never seen this hand-over ---
    restarted = orchestrator(db_path, backend)
    try:
        recovered = restarted.projects.get(project_id)
        assert recovered.status is ProjectStatus.ACTIVE
        assert recovered.assigned_agent_id == agent_id
        assert restarted.agents.for_project(project_id).assignment.handle == handle

        settled = await restarted.settle(project_id, timeout=1200.0, interval=5.0)

        assert settled.status is ProjectStatus.COMPLETED, (
            f"project ended {settled.status.value}: {settled.summary} {settled.blockers}"
        )
        assert settled.summary
        # One Project, one Agent, still — the restart adopted it rather than
        # starting a second one.
        agents = restarted.agent_store.for_project(project_id)
        assert [a.agent_id for a in agents] == [agent_id]
        assert agents[0].status is AgentStatus.IDLE
        inbound = [m for d, m in restarted.gateway.history(project_id) if d == "inbound"]
        assert A2AMessageType.PROJECT_COMPLETED in {m.type for m in inbound}
    finally:
        restarted.close()


async def test_real_agent_blocked_project_resumes_from_a_follow_up(tmp_path):
    """The whole point: a person answers, and the same Project carries on."""
    db_path = tmp_path / "o.db"
    backend = FakeLLMBackend(default=create(SAP_REQUEST))
    orch = orchestrator(db_path, backend)
    try:
        _decision, project = await orch.submit(SAP_REQUEST)
        assert project is not None
        project = await orch.settle(project.id, timeout=1200.0, interval=5.0)

        assert project.status in (ProjectStatus.BLOCKED, ProjectStatus.WAITING_HUMAN), (
            "the agent claimed a goal it had no way to reach: " + project.summary
        )
        assert project.current_blockers
        escalations = {
            m.type for d, m in orch.gateway.history(project.id) if d == "inbound"
        } & TERMINAL_ESCALATIONS
        assert escalations
        agent_id = project.assigned_agent_id

        # --- the person answers ---
        backend.default = add_task(project.id, FOLLOW_UP)
        _decision, resumed = await orch.submit(FOLLOW_UP)

        assert resumed is not None and resumed.id == project.id
        assert len(orch.projects.all()) == 1, "a follow-up must not start a new project"
        assert resumed.assigned_agent_id == agent_id
        assert resumed.current_blockers == []
        # What blocked it stays in the record, marked resolved by the follow-up.
        assert any(blocker.get("resolved_at") for blocker in resumed.blockers)

        finished = await orch.settle(resumed.id, timeout=1200.0, interval=5.0)

        assert finished.status is ProjectStatus.COMPLETED, (
            f"resumed project ended {finished.status.value}: {finished.summary}"
        )
        assert [a.agent_id for a in orch.agent_store.for_project(project.id)] == [agent_id]
    finally:
        orch.close()
