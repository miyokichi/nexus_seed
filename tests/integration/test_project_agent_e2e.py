"""End-to-end: a real external Project Agent carries out a whole Project.

These are the only tests that need another process running.  They are skipped
unless one is pointed at::

    little-agent --serve-a2a --agent analysis_worker --port 8801 --auto-approve
    $env:NEXUS_SEED_PROJECT_AGENT_URL = "http://127.0.0.1:8801"
    pytest tests/integration

Everything else in the suite runs network-free against ``InProcessAgentRuntime``
or a scripted A2A server, so a plain ``pytest`` never depends on an agent being
up.

What is asserted here is the *orchestration contract* — a project exists, one
agent owns it, the goal really went out, what came back moved the project —
never the wording of an answer a real model produced.
"""

from __future__ import annotations

import os

import pytest

from nexus_seed.orchestrator import (
    A2AMessageType,
    AgentStatus,
    ProjectOrchestrator,
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

TERMINAL_ESCALATIONS = {
    A2AMessageType.NEED_CAPABILITY,
    A2AMessageType.NEED_RESOURCE,
    A2AMessageType.NEED_PERMISSION,
    A2AMessageType.PROJECT_BLOCKED,
    A2AMessageType.NEED_HUMAN_INPUT,
}


def settings() -> ProjectAgentSettings:
    """Real Project Agent settings, whatever the developer's .env happens to say."""
    return ProjectAgentSettings(runtime="a2a", url=AGENT_URL)


def orchestrator(db_path) -> ProjectOrchestrator:
    return build_orchestrator(db_path, settings=settings())


def inbound(orch: ProjectOrchestrator, project_id: str) -> list:
    return [m for direction, m in orch.gateway.history(project_id) if direction == "inbound"]


async def test_real_agent_project_completed(tmp_path):
    db_path = tmp_path / "o.db"
    orch = orchestrator(db_path)
    try:
        decision, project = await orch.submit(ANALYSIS_REQUEST)

        assert project is not None, f"no project was created: {decision.to_dict()}"
        assert len(orch.projects.all()) == 1
        assert project.status is ProjectStatus.COMPLETED, (
            f"project ended {project.status.value}: {project.summary} "
            f"{project.blockers}"
        )
        assert project.summary, "a completed project must carry the agent's summary"

        # Exactly one Agent owned it, and it is free again.
        agents = orch.agent_store.for_project(project.id)
        assert len(agents) == 1
        assert agents[0].runtime == "a2a"
        assert agents[0].status is AgentStatus.IDLE
        assert project.assigned_agent_id == agents[0].agent_id

        # The goal really went over A2A and the answer is on the audited channel.
        received = inbound(orch, project.id)
        assert A2AMessageType.PROJECT_COMPLETED in {m.type for m in received}
        assert all(m.source_agent_id == agents[0].agent_id for m in received)
    finally:
        orch.close()

    # --- and it is all still there after a restart ---
    restarted = orchestrator(db_path)
    try:
        [recovered] = restarted.projects.all()
        assert recovered.status is ProjectStatus.COMPLETED
        assert recovered.assigned_agent_id is not None
        assert inbound(restarted, recovered.id)
    finally:
        restarted.close()


async def test_real_agent_need_capability_blocks_project(tmp_path):
    db_path = tmp_path / "o.db"
    orch = orchestrator(db_path)
    try:
        _decision, project = await orch.submit(SAP_REQUEST)

        assert project is not None
        # The agent has no SAP access and cannot get it, so it must come back
        # rather than substitute another source or retry forever.
        assert project.status is not ProjectStatus.COMPLETED, (
            "the agent claimed a goal it had no way to reach: " + project.summary
        )
        assert project.status in (ProjectStatus.BLOCKED, ProjectStatus.WAITING_HUMAN)
        assert project.blockers, "a blocked project must record why"
        assert project.blockers[0]["reason"]

        escalations = {m.type for m in inbound(orch, project.id)} & TERMINAL_ESCALATIONS
        assert escalations, "the agent stopped without saying what it was missing"
    finally:
        orch.close()

    restarted = orchestrator(db_path)
    try:
        [recovered] = restarted.projects.all()
        assert recovered.status in (ProjectStatus.BLOCKED, ProjectStatus.WAITING_HUMAN)
        assert recovered.blockers
    finally:
        restarted.close()
