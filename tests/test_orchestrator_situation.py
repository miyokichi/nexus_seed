"""An orchestrator Project read through the same surface as a Goal-rooted one.

Project Chat, ``GET /projects/{id}/situation`` and the Cockpit all speak
ProjectSituation.  An orchestrator Project has to arrive there too — otherwise
a person can only ask questions about the kind of project that is going away.
What it must *not* do is invent progress: the Agent owns the execution, so the
situation reports what NEXUS SEED actually knows and nothing more.
"""

from __future__ import annotations

import pytest

from nexus_seed.backends import proposal_response
from nexus_seed.chat.service import ProjectChatService
from nexus_seed.orchestrator import (
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    completed_message,
    escalation,
)
from nexus_seed.orchestrator.situation import (
    orchestrator_situation,
    orchestrator_situations,
)
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.projects.models import ProjectOverallStatus
from nexus_seed.projects.projections import get_project_situation, get_project_summaries
from nexus_seed.runtime.runtime import Runtime


pytestmark = pytest.mark.asyncio

REQUEST = "7月の売上低下原因を調べて"


def decision(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


class ScriptedBackend:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, request):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


async def setup(tmp_path, behaviour, name="situation.db"):
    runtime = Runtime(tmp_path / name)
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=ScriptedBackend([decision("CREATE_PROJECT", proposed_goal=REQUEST)]),
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    await orch.handle_request(REQUEST)
    return runtime, orch, orch.projects.all()[0]


quiet = lambda config, envelope: []


def blocking(config, envelope):
    return [
        escalation(
            config.project_id,
            A2AMessageType.NEED_CAPABILITY,
            required_capability="sap",
            reason="SAP accessがない",
        )
    ]


async def test_a_delegated_project_reads_as_an_active_situation(tmp_path):
    runtime, _orch, project = await setup(tmp_path, quiet)
    try:
        situation = orchestrator_situation(runtime, project.id)
        assert situation is not None
        assert situation.project_id == project.id
        assert situation.objective == REQUEST
        assert situation.overall_status is ProjectOverallStatus.ACTIVE
        # The Agent owns the breakdown, so nothing here claims task progress.
        assert situation.completed_total == 0
    finally:
        runtime.close()


async def test_a_blocked_project_reports_the_escalation_as_a_blocker(tmp_path):
    runtime, orch, project = await setup(tmp_path, blocking, "blocked.db")
    try:
        assert orch.projects.get(project.id).status is ProjectStatus.BLOCKED
        situation = orchestrator_situation(runtime, project.id)
        assert situation.overall_status is ProjectOverallStatus.BLOCKED
        assert [item["summary"] for item in situation.blockers] == ["SAP accessがない"]
        assert situation.blockers[0]["type"] == "NEED_CAPABILITY"
    finally:
        runtime.close()


async def test_a_completed_project_counts_its_tasks_as_done(tmp_path):
    done = lambda config, envelope: [completed_message(config.project_id, "終わりました")]
    runtime, orch, project = await setup(tmp_path, done, "done.db")
    try:
        settled = orch.projects.get(project.id)
        assert settled.status is ProjectStatus.COMPLETED
        situation = orchestrator_situation(runtime, project.id)
        assert situation.overall_status is ProjectOverallStatus.COMPLETED
        assert situation.summary == "終わりました"
        assert situation.remaining_tasks == ()
        assert situation.completed_total == len(settled.tasks)
    finally:
        runtime.close()


async def test_the_a2a_channel_is_what_recent_changes_are(tmp_path):
    runtime, _orch, project = await setup(tmp_path, blocking, "changes.db")
    try:
        situation = orchestrator_situation(runtime, project.id)
        assert situation.recent_changes
        assert "SAP accessがない" in situation.recent_changes[-1]["summary"]
        assert all(
            item["type"].startswith("a2a.") for item in situation.recent_events
        )
    finally:
        runtime.close()


async def test_the_shared_project_lookup_resolves_an_orchestrator_project(tmp_path):
    runtime, _orch, project = await setup(tmp_path, quiet, "shared.db")
    try:
        situation = get_project_situation(runtime, project.id)
        assert situation is not None
        assert situation.project_id == project.id
        assert get_project_situation(runtime, "no-such-project") is None
    finally:
        runtime.close()


async def test_orchestrator_projects_are_known_to_the_scope_guard(tmp_path):
    runtime, _orch, project = await setup(tmp_path, quiet, "scope.db")
    try:
        [summary] = get_project_summaries(runtime)
        assert summary.project_id == project.id
        assert summary.objective == REQUEST
    finally:
        runtime.close()


async def test_every_orchestrator_project_can_be_listed(tmp_path):
    runtime, orch, project = await setup(tmp_path, quiet, "list.db")
    try:
        await orch.handle_request("別件も調べて")
        ids = {item.project_id for item in orchestrator_situations(runtime)}
        assert project.id in ids
        assert len(ids) == len(orch.projects.all())
    finally:
        runtime.close()


async def test_a_question_about_an_orchestrator_project_is_answered(tmp_path):
    """The chat thread is the same journal, on the same read-only path."""

    runtime, _orch, project = await setup(tmp_path, blocking, "chat.db")
    try:
        chat = ProjectChatService(runtime)
        reply = await chat.ask(project.id, "今どうなってる？")
        assert reply is not None
        assert reply["project_id"] == project.id
        assert reply["answer"]
        history = chat.history(project.id)
        assert [item["role"] for item in history["messages"]] == ["HUMAN", "NEXUS_SEED"]
    finally:
        runtime.close()


async def test_cockpit_renders_an_ask_panel_for_orchestrator_projects():
    from nexus_seed.cockpit.assets import APP_JS

    assert "orchestrator-chat-form" in APP_JS
    assert "askOrchestrator" in APP_JS
