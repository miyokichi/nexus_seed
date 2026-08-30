"""One-shot Observer -> Knowledge -> Planner -> A2A closed-loop acceptance tests."""

from __future__ import annotations

from pathlib import Path

from nexus_seed.app.flows import ClosedLoopMVPApplication, ClosedLoopRequest
from nexus_seed.modules.knowledge import ExistingKnowledgeGateway, KnowledgeLedger
from nexus_seed.modules.knowledge.adapters.sqlite import KnowledgeStore
from nexus_seed.modules.observer import TextObserver
from nexus_seed.modules.project_manager import (
    A2AAgentRuntime,
    A2AMessage,
    A2AMessageType,
    Dispatch,
    ProjectOrchestrator,
    ProjectStatus,
)
from nexus_seed.modules.project_manager.workspace import GrantPolicy
from nexus_seed.policy.approval.mvp import FixedApproval
from nexus_seed.platform.contracts.mvp import KnowledgeItem, ProjectProposal
from nexus_seed.resources.scope import ResourceScope
from nexus_seed.storage import Database


class FixturePlanner:
    """A deterministic Planner implementation using the public MVP contract."""

    def __init__(self, proposals: list[ProjectProposal]) -> None:
        self.proposals = list(proposals)
        self.contexts: list[KnowledgeItem] = []

    def propose(self, context: KnowledgeItem) -> list[ProjectProposal]:
        self.contexts.append(context)
        return list(self.proposals)


class FixtureA2ATransport:
    """A deterministic external Agent fixture behind the normal A2A runtime."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.opened = []
        self.sent = []
        self.resource_checks: dict[str, bool] = {}

    async def open(self, config):
        self.opened.append(config)
        return "a2a://fixture-little-agent"

    async def start(self, config, envelope):
        self.sent.append((config, envelope))
        if self.fail:
            return Dispatch(
                messages=[
                    A2AMessage(
                        type=A2AMessageType.PROJECT_BLOCKED,
                        project_id=config.project_id,
                        source_agent_id=config.agent_id,
                        payload={
                            "reason": "fixture execution failed",
                            "error": "deterministic agent failure",
                        },
                    )
                ]
            )

        original = Path(config.readable_paths[0])
        workspace = Path(config.workspace or "")
        output = workspace / "performance_report.txt"
        outside = workspace.parent / "outside.txt"
        agent_scope = ResourceScope(
            read_roots=[original.parent], write_roots=[workspace]
        )
        self.resource_checks = {
            "read_original": agent_scope.can_read(original),
            "write_original": agent_scope.can_write(original),
            "write_workspace": agent_scope.can_write(output),
            "write_outside": agent_scope.can_write(outside),
        }
        if not self.resource_checks["write_workspace"]:
            raise AssertionError("fixture agent did not receive a writable workspace")
        output.write_text("Performance summary: stable\n", encoding="utf-8")
        return Dispatch(
            messages=[
                A2AMessage(
                    type=A2AMessageType.PROJECT_COMPLETED,
                    project_id=config.project_id,
                    source_agent_id=config.agent_id,
                    payload={
                        "summary": "Read report.txt and wrote performance_report.txt.",
                        "output_file": "performance_report.txt",
                    },
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


def _application(tmp_path, planner, transport):
    original_root = tmp_path / "original"
    original_root.mkdir()
    report = original_root / "report.txt"
    report.write_text("Latency is below target.\n", encoding="utf-8")
    database = Database(tmp_path / "nexus.db")
    knowledge = ExistingKnowledgeGateway(KnowledgeLedger(KnowledgeStore(database)))
    orchestrator = ProjectOrchestrator(
        database,
        agent_runtime=A2AAgentRuntime(transport),
        workspace_root=str(tmp_path / "workspaces"),
        grant_policy=GrantPolicy(scope=ResourceScope(read_roots=[original_root])),
    )
    application = ClosedLoopMVPApplication(
        observer=TextObserver(
            "report.txt was updated; verify the performance conclusion.",
            source="manual",
            metadata={"resource": "report.txt"},
        ),
        knowledge=knowledge,
        planner=planner,
        approval=FixedApproval(True),
        project_manager=orchestrator,
    )
    return application, database, knowledge, orchestrator, report


async def test_closed_loop_action_required_executes_through_a2a_and_records_result(tmp_path):
    proposal = ProjectProposal(
        id="plan-performance-report",
        title="Verify performance report",
        goal="Read report.txt and write performance_report.txt in the workspace.",
        reason="The observation names a changed performance report.",
    )
    planner = FixturePlanner([proposal])
    transport = FixtureA2ATransport()
    application, database, knowledge, orchestrator, report_file = _application(
        tmp_path, planner, transport
    )
    try:
        report = await application.run_once(
            ClosedLoopRequest(
                goal="Verify the performance conclusion and record a concise report.",
                resource_uris=(f"file:{report_file}",),
            )
        )

        assert report.state == "COMPLETED"
        assert report.project is not None
        assert report.project.status is ProjectStatus.COMPLETED
        assert report.execution_result is not None
        assert report.execution_result.payload["output_file"] == "performance_report.txt"
        assert report.result_observation is not None
        assert report.result_observation.source == "project_result"
        assert report.result_observation.metadata["proposal_id"] == proposal.id
        assert (tmp_path / "workspaces" / report.project.id / "performance_report.txt").read_text(
            encoding="utf-8"
        ) == "Performance summary: stable\n"
        assert report_file.read_text(encoding="utf-8") == "Latency is below target.\n"
        assert len(transport.opened) == len(transport.sent) == 1
        assert transport.sent[0][1]["kind"] == "ASSIGN_GOAL"
        assert planner.contexts[0].content["goal"].startswith("Verify the performance")
        assert planner.contexts[0].content["observation"]["id"] == report.observation.id
        assert planner.contexts[0].content["relevant_knowledge"][0]["id"] == report.observation.id
        assert knowledge.get(report.observation.id) is not None
        assert knowledge.get(report.result_observation.id) is not None
        inbound = [
            message
            for direction, message in orchestrator.gateway.history(report.project.id)
            if direction == "inbound"
        ]
        assert [message.type for message in inbound] == [A2AMessageType.PROJECT_COMPLETED]
    finally:
        orchestrator.close()
        database.close()


async def test_closed_loop_no_action_stops_before_project_or_a2a(tmp_path):
    planner = FixturePlanner([])
    transport = FixtureA2ATransport()
    application, database, knowledge, orchestrator, _report_file = _application(
        tmp_path, planner, transport
    )
    try:
        report = await application.run_once(ClosedLoopRequest(goal="Check whether action is needed."))

        assert report.state == "NO_ACTION"
        assert report.project is None
        assert report.result_observation is None
        assert transport.opened == []
        assert transport.sent == []
        assert knowledge.get(report.observation.id) is not None
        assert len(knowledge.ledger.all_heads()) == 1
    finally:
        orchestrator.close()
        database.close()


async def test_closed_loop_execution_failure_stays_in_project_lifecycle_and_knowledge(tmp_path):
    planner = FixturePlanner(
        [
            ProjectProposal(
                title="Run failing fixture",
                goal="Attempt the deterministic execution.",
                reason="Exercise the returned failure path.",
            )
        ]
    )
    transport = FixtureA2ATransport(fail=True)
    application, database, knowledge, orchestrator, _report_file = _application(
        tmp_path, planner, transport
    )
    try:
        report = await application.run_once(ClosedLoopRequest(goal="Exercise a failure result."))

        assert report.state == "EXECUTION_STOPPED"
        assert report.project is not None
        assert report.project.status is ProjectStatus.BLOCKED
        assert report.project.status is not ProjectStatus.COMPLETED
        assert report.execution_result is not None
        assert report.execution_result.payload["error"] == "deterministic agent failure"
        assert report.result_observation is not None
        assert report.result_observation.content["status"] == "blocked"
        assert knowledge.get(report.result_observation.id) is not None
        [message] = [
            message
            for direction, message in orchestrator.gateway.history(report.project.id)
            if direction == "inbound"
        ]
        assert message.type is A2AMessageType.PROJECT_BLOCKED
        assert message.payload["error"] == "deterministic agent failure"
    finally:
        orchestrator.close()
        database.close()


async def test_closed_loop_resource_boundary_is_preserved_at_a2a_handover(tmp_path):
    planner = FixturePlanner(
        [ProjectProposal(title="Resource check", goal="Read report and write output.", reason="test")]
    )
    transport = FixtureA2ATransport()
    application, database, _knowledge, orchestrator, report_file = _application(
        tmp_path, planner, transport
    )
    try:
        report = await application.run_once(
            ClosedLoopRequest(goal="Check the supplied report.", resource_uris=(f"file:{report_file}",))
        )

        assert report.state == "COMPLETED"
        assert transport.resource_checks == {
            "read_original": True,
            "write_original": False,
            "write_workspace": True,
            "write_outside": False,
        }
        assert not (tmp_path / "workspaces" / "outside.txt").exists()
    finally:
        orchestrator.close()
        database.close()
