"""Bounded Observation -> execution -> World View replanning acceptance tests."""

from __future__ import annotations

from pathlib import Path

from nexus_seed.app.flows import ClosedLoopMVPApplication, ClosedLoopRequest
from nexus_seed.modules.knowledge import ExistingKnowledgeGateway, KnowledgeLedger
from nexus_seed.modules.knowledge.adapters.sqlite import KnowledgeStore
from nexus_seed.modules.knowledge.projection import (
    WorldStateProjection,
    annotate_world_fact,
)
from nexus_seed.modules.observer import TextObserver
from nexus_seed.modules.project_manager import (
    A2AAgentRuntime,
    A2AMessage,
    A2AMessageType,
    Dispatch,
    ProjectOrchestrator,
)
from nexus_seed.platform.contracts.mvp import KnowledgeItem, ProjectProposal
from nexus_seed.policy.approval.mvp import FixedApproval
from nexus_seed.storage import Database


class SequencePlanner:
    """Propose one deterministic Project for a bounded number of calls."""

    def __init__(self, action_count: int) -> None:
        self.action_count = action_count
        self.contexts: list[KnowledgeItem] = []

    def propose(self, context: KnowledgeItem) -> list[ProjectProposal]:
        self.contexts.append(context)
        call = len(self.contexts)
        if call > self.action_count:
            return []
        return [
            ProjectProposal(
                id=f"finite-loop-proposal-{call}",
                title=f"Finite loop step {call}",
                goal="Create or update performance_report.txt.",
                reason="The current World View still requires work.",
            )
        ]


class SequenceA2ATransport:
    """Return explicit World Facts through the ordinary A2A result channel."""

    def __init__(self, outcomes: list[list[dict] | str | None]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def open(self, _config):
        return "a2a://finite-loop-fixture"

    async def start(self, config, _envelope):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if outcome == "blocked":
            return Dispatch(
                messages=[
                    A2AMessage(
                        type=A2AMessageType.PROJECT_BLOCKED,
                        project_id=config.project_id,
                        source_agent_id=config.agent_id,
                        payload={"reason": "fixture is blocked"},
                    )
                ]
            )

        workspace = Path(config.workspace or "")
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "performance_report.txt").write_text(
            f"Performance report iteration {self.calls}\n",
            encoding="utf-8",
        )
        payload = {
            "summary": "Created performance_report.txt.",
            "output_file": "performance_report.txt",
        }
        if outcome is not None:
            payload["world_facts"] = outcome
        return Dispatch(
            messages=[
                A2AMessage(
                    type=A2AMessageType.PROJECT_COMPLETED,
                    project_id=config.project_id,
                    source_agent_id=config.agent_id,
                    payload=payload,
                )
            ]
        )

    async def collect(self, _config, _handle):
        return []

    async def abandon(self, _handle):
        return True

    async def close(self, _agent_id):
        return None

    async def alive(self):
        return True


def _fact(entity: str, attribute: str, value) -> dict:
    return {"entity": entity, "attribute": attribute, "value": value}


def _seed_fact(
    ledger: KnowledgeLedger,
    knowledge_id: str,
    entity: str,
    attribute: str,
    value,
) -> None:
    ledger.record(
        _fact(entity, attribute, value),
        source_type="test",
        knowledge_id=knowledge_id,
        format="json",
    )
    annotate_world_fact(
        ledger,
        knowledge_id,
        entity=entity,
        attribute=attribute,
        value=value,
        created_by="finite_loop_fixture",
    )


def _application(tmp_path, *, actions: int, outcomes: list[list[dict] | str | None]):
    database = Database(tmp_path / "nexus.db")
    ledger = KnowledgeLedger(KnowledgeStore(database))
    knowledge = ExistingKnowledgeGateway(ledger)
    planner = SequencePlanner(actions)
    transport = SequenceA2ATransport(outcomes)
    orchestrator = ProjectOrchestrator(
        database,
        agent_runtime=A2AAgentRuntime(transport),
        workspace_root=str(tmp_path / "workspaces"),
    )
    application = ClosedLoopMVPApplication(
        observer=TextObserver("A performance evaluation was requested.", source="manual"),
        knowledge=knowledge,
        planner=planner,
        approval=FixedApproval(True),
        project_manager=orchestrator,
    )
    return application, database, ledger, knowledge, planner, transport, orchestrator


async def test_two_step_execution_replans_from_world_diff_then_stops_no_action(tmp_path):
    app, database, ledger, knowledge, planner, transport, orchestrator = _application(
        tmp_path,
        actions=1,
        outcomes=[[_fact("performance_report", "status", "created")]],
    )
    _seed_fact(ledger, "review-status", "review", "status", "completed")
    _seed_fact(
        ledger,
        "performance-report-status",
        "performance_report",
        "status",
        "missing",
    )
    try:
        report = await app.run_until_stable(
            ClosedLoopRequest(goal="Finish the performance evaluation."),
        )

        assert report.status == "stable"
        assert report.iterations == 2
        assert report.stop_reason == "no_action"
        assert len(report.project_ids) == 1
        assert transport.calls == 1
        assert WorldStateProjection(ledger).view().snapshot() == {
            "performance_report": {"status": "created"},
            "review": {"status": "completed"},
        }
        [change] = report.last_world_diff.changes
        assert (change.entity, change.attribute, change.old_value, change.new_value) == (
            "performance_report",
            "status",
            "missing",
            "created",
        )
        second_context = planner.contexts[1].content
        assert second_context["observation"]["source"] == "knowledge_world_diff"
        assert second_context["observation"]["content"]["world_changes"][0] == {
            "entity": "performance_report",
            "attribute": "status",
            "old_value": "missing",
            "new_value": "created",
            "change": "changed",
        }
        first_run = report.runs[0]
        assert first_run.result_observation is not None
        assert knowledge.get(first_run.result_observation.id) is not None
        assert (
            tmp_path
            / "workspaces"
            / report.project_ids[0]
            / "performance_report.txt"
        ).exists()
    finally:
        orchestrator.close()
        database.close()


async def test_completed_project_without_new_world_fact_stops_stable_world(tmp_path):
    app, database, _ledger, _knowledge, planner, transport, orchestrator = _application(
        tmp_path,
        actions=2,
        outcomes=[None],
    )
    try:
        report = await app.run_until_stable(ClosedLoopRequest(goal="Run one stable step."))

        assert report.status == "stable"
        assert report.iterations == 1
        assert report.stop_reason == "stable_world"
        assert not report.last_world_diff
        assert len(planner.contexts) == transport.calls == 1
    finally:
        orchestrator.close()
        database.close()


async def test_blocked_project_stops_without_retry(tmp_path):
    app, database, _ledger, _knowledge, planner, transport, orchestrator = _application(
        tmp_path,
        actions=2,
        outcomes=["blocked"],
    )
    try:
        report = await app.run_until_stable(ClosedLoopRequest(goal="Run blocked work."))

        assert report.status == "blocked"
        assert report.iterations == 1
        assert report.stop_reason == "blocked"
        assert len(report.project_ids) == 1
        assert len(planner.contexts) == transport.calls == 1
    finally:
        orchestrator.close()
        database.close()


async def test_novel_world_changes_stop_at_max_iterations(tmp_path):
    app, database, ledger, _knowledge, planner, transport, orchestrator = _application(
        tmp_path,
        actions=3,
        outcomes=[
            [_fact("evaluation", "revision", 1)],
            [_fact("evaluation", "revision", 2)],
            [_fact("evaluation", "revision", 3)],
        ],
    )
    _seed_fact(ledger, "evaluation-revision", "evaluation", "revision", 0)
    try:
        report = await app.run_until_stable(
            ClosedLoopRequest(goal="Advance the evaluation."),
            max_iterations=3,
        )

        assert report.status == "limit_reached"
        assert report.iterations == 3
        assert report.stop_reason == "max_iterations"
        assert len(report.project_ids) == 3
        assert len(planner.contexts) == transport.calls == 3
        assert WorldStateProjection(ledger).view().get("evaluation", "revision") == 3
    finally:
        orchestrator.close()
        database.close()


async def test_revisited_world_state_stops_repeated_state(tmp_path):
    app, database, ledger, _knowledge, planner, transport, orchestrator = _application(
        tmp_path,
        actions=3,
        outcomes=[
            [_fact("switch", "status", "B")],
            [_fact("switch", "status", "A")],
        ],
    )
    _seed_fact(ledger, "switch-status", "switch", "status", "A")
    try:
        report = await app.run_until_stable(
            ClosedLoopRequest(goal="Settle the switch state."),
            max_iterations=3,
        )

        assert report.status == "stopped"
        assert report.iterations == 2
        assert report.stop_reason == "repeated_state"
        assert len(report.project_ids) == 2
        assert len(planner.contexts) == transport.calls == 2
        assert WorldStateProjection(ledger).view().get("switch", "status") == "A"
    finally:
        orchestrator.close()
        database.close()
