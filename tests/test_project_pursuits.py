"""Projects are what NEXUS SEED pursues, and Phase 6 intends about them.

A Project is the goal: the objective a person handed over, delegated whole to
one Agent.  Nothing sits between the two to decompose — the Agent breaks the
Project into tasks itself — so an Intention is held about the Project directly.
"""

from __future__ import annotations

import pytest

from nexus_seed.backends import proposal_response
from nexus_seed.core.event import Event
from nexus_seed.orchestrator import (
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
    completed_message,
    escalation,
)
from nexus_seed.orchestrator.pursuits import ProjectPursuits
from nexus_seed.presence import get_intention_for_pursuit, project_self
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


pytestmark = pytest.mark.asyncio

REQUEST = "7月の売上低下原因を調べて"


def routing(action, **fields):
    return proposal_response({"action": action, "reason": "t", "confidence": 0.9, **fields})


class Scripted:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, request):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


quiet = lambda config, envelope: []


async def orchestrated(tmp_path, behaviour=quiet, name="pursuits.db", phase6=True):
    runtime = Runtime(tmp_path / name)
    bootstrap_semantic(runtime)
    orch = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=Scripted([routing("CREATE_PROJECT", proposed_goal=REQUEST)]),
    )
    bootstrap_project_orchestration(runtime, orch, enabled=True)
    bootstrap_persistent_being(runtime, enabled=phase6, wake_on_start=False)
    return runtime, orch


async def test_a_live_project_is_a_pursuit(tmp_path):
    runtime, orch = await orchestrated(tmp_path)
    try:
        await orch.handle_request(REQUEST)
        [project] = orch.projects.all()

        [pursuit] = runtime.active_pursuits()
        assert pursuit.id == project.id
        assert pursuit.objective == REQUEST
        assert pursuit.active is True
        assert project_self(runtime).active_pursuit_ids == (project.id,)
    finally:
        runtime.close()


async def test_a_finished_project_stops_being_pursued_but_stays_resolvable(tmp_path):
    done = lambda config, envelope: [completed_message(config.project_id, "終わりました")]
    runtime, orch = await orchestrated(tmp_path, done, "done.db")
    try:
        await orch.handle_request(REQUEST)
        [project] = orch.projects.all()
        assert orch.projects.get(project.id).status is ProjectStatus.COMPLETED

        assert runtime.active_pursuits() == []
        # The Intention about it must still be able to describe what it was for.
        finished = runtime.get_pursuit(project.id)
        assert finished is not None and finished.active is False
        assert finished.objective == REQUEST
    finally:
        runtime.close()


async def test_a_blocked_project_is_still_pursued(tmp_path):
    def blocking(config, envelope):
        return [
            escalation(
                config.project_id,
                A2AMessageType.NEED_CAPABILITY,
                required_capability="sap",
                reason="SAP accessがない",
            )
        ]

    runtime, orch = await orchestrated(tmp_path, blocking, "blocked.db")
    try:
        await orch.handle_request(REQUEST)
        [project] = orch.projects.all()
        assert orch.projects.get(project.id).status is ProjectStatus.BLOCKED
        # Blocked is not finished: NEXUS SEED is still trying to achieve it.
        assert [item.id for item in runtime.active_pursuits()] == [project.id]
    finally:
        runtime.close()


async def test_phase6_holds_an_intention_about_the_project_itself(tmp_path):
    runtime, orch = await orchestrated(tmp_path, name="intention.db")
    try:
        await orch.handle_request(REQUEST)
        [project] = orch.projects.all()

        await runtime.submit_event(
            Event(
                "intention_declaration_requested",
                "test",
                {"goal_id": project.id, "focus": "原因を突き止める"},
            )
        )

        intention = get_intention_for_pursuit(runtime, project.id)
        assert intention is not None
        assert intention.pursuit_id == project.id
        assert intention.focus == "原因を突き止める"
        # No Goal was created anywhere along the way.
        assert runtime.control_store.goals() == []
    finally:
        runtime.close()


async def test_a_project_can_declare_what_should_reopen_its_intention(tmp_path):
    runtime, orch = await orchestrated(tmp_path, name="reconsider.db")
    try:
        await orch.handle_request(REQUEST)
        [project] = orch.projects.all()
        project.context = {**project.context, "reconsider_on": ["external_signal"]}
        orch.projects.store.save(project)

        [pursuit] = runtime.active_pursuits()
        assert pursuit.reconsider_on == ("external_signal",)
    finally:
        runtime.close()


async def test_the_source_is_not_registered_when_the_orchestrator_is_off(tmp_path):
    runtime = Runtime(tmp_path / "off.db")
    try:
        assert bootstrap_project_orchestration(runtime, None, enabled=False) is False
        assert runtime.pursuit_sources == []
        assert runtime.active_pursuits() == []
    finally:
        runtime.close()


async def test_the_source_reads_the_store_it_was_given(tmp_path):
    """No caching: what a Project is now is what the pursuit says now."""

    runtime, orch = await orchestrated(tmp_path, name="fresh.db")
    try:
        source = ProjectPursuits(orch.projects)
        assert source.live() == []
        await orch.handle_request(REQUEST)
        assert len(source.live()) == 1
    finally:
        runtime.close()
