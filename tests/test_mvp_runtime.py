"""MVP acceptance loop using only replaceable test doubles."""

from __future__ import annotations

from nexus_seed.mvp import (
    DefaultProjectExecutor,
    ExistingKnowledgeGateway,
    ExistingProjectManagerAdapter,
    FixedApproval,
    MVPApplicationRuntime,
    Observation,
    ProjectProposal,
    ProjectResult,
    ProjectResultStatus,
    ProjectStatus,
    SimpleProjectPlanner,
    TextObserver,
)
from nexus_seed.mvp.testing import (
    MockKnowledgeGateway,
    MockObserver,
    MockProjectExecutor,
    MockProjectManager,
    MockProjectPlanner,
    MockLLMProvider,
)
from nexus_seed.knowledge import KnowledgeLedger
from nexus_seed.orchestrator.project_manager import ProjectManager as DurableProjectManager
from nexus_seed.storage import Database, KnowledgeStore
from nexus_seed.storage.orchestrator_store import ProjectStore


def test_mvp_runtime_completes_the_readme_loop_with_replaceable_modules():
    observation = Observation(
        id="observation-readme",
        content="NEXUS SEED の README を改善する。",
        source="human",
    )
    proposal = ProjectProposal(
        id="proposal-readme",
        title="README改善",
        goal="READMEを確認し、改善案を作成する",
        reason="READMEを改善する依頼を観測したため",
        context={"observation_id": observation.id},
    )
    knowledge = MockKnowledgeGateway()
    executor = MockProjectExecutor(
        ProjectResult(
            project_id="replaced-by-mock",
            status=ProjectResultStatus.COMPLETED,
            summary="READMEの改善案を作成しました",
            outputs={"suggestions": ["add quick start"]},
        )
    )
    manager = MockProjectManager()
    runtime = MVPApplicationRuntime(
        observer=MockObserver([observation]),
        knowledge=knowledge,
        planner=MockProjectPlanner([proposal]),
        approval=FixedApproval(True),
        project_manager=manager,
        executor=executor,
        workspace=".",
    )

    report = runtime.run()

    assert len(report.observations) == 1
    assert len(report.proposals) == 1
    assert report.projects[0].status is ProjectStatus.COMPLETED
    assert report.results[0].summary == "READMEの改善案を作成しました"
    assert knowledge.get(observation.id) is not None
    assert knowledge.get(f"result-{report.projects[0].id}") is not None
    assert executor.calls[0].goal == proposal.goal


def test_rejected_proposal_never_creates_or_executes_a_project():
    observation = Observation(content="do something", source="human")
    proposal = ProjectProposal(title="work", goal="work", reason="request")
    manager = MockProjectManager()
    executor = MockProjectExecutor()
    runtime = MVPApplicationRuntime(
        observer=MockObserver([observation]),
        knowledge=MockKnowledgeGateway(),
        planner=MockProjectPlanner([proposal]),
        approval=FixedApproval(False),
        project_manager=manager,
        executor=executor,
    )

    report = runtime.run()

    assert report.rejected_proposal_ids == (proposal.id,)
    assert manager.list() == []
    assert executor.calls == []


def test_mvp_durable_e2e_completes_and_returns_result_to_knowledge(tmp_path):
    db = Database(tmp_path / "mvp.db")
    try:
        ledger = KnowledgeLedger(KnowledgeStore(db))
        knowledge = ExistingKnowledgeGateway(ledger)
        manager = ExistingProjectManagerAdapter(
            DurableProjectManager(ProjectStore(db))
        )
        runtime = MVPApplicationRuntime(
            observer=TextObserver(
                "NEXUS SEED の README を改善する。", source="human"
            ),
            knowledge=knowledge,
            planner=SimpleProjectPlanner(),
            approval=FixedApproval(True),
            project_manager=manager,
            executor=DefaultProjectExecutor(
                MockLLMProvider(
                    default={
                        "summary": "READMEの改善案を作成しました",
                        "outputs": {"suggestions": ["add quick start"]},
                    }
                )
            ),
            workspace=str(tmp_path),
        )

        report = runtime.run()

        project = report.projects[0]
        assert project.title == "README改善"
        assert project.status is ProjectStatus.COMPLETED
        assert manager.get(project.id).result.summary == "READMEの改善案を作成しました"
        result_knowledge = knowledge.get(f"result-{project.id}")
        assert result_knowledge.content["summary"] == "READMEの改善案を作成しました"
        assert len(ledger.all_heads()) == 2
    finally:
        db.close()


def test_failed_executor_marks_project_failed_and_records_same_meaning(tmp_path):
    db = Database(tmp_path / "failed-mvp.db")
    try:
        ledger = KnowledgeLedger(KnowledgeStore(db))
        knowledge = ExistingKnowledgeGateway(ledger)
        manager = ExistingProjectManagerAdapter(
            DurableProjectManager(ProjectStore(db))
        )
        executor_result = ProjectResult(
            project_id="assigned-by-runtime",
            status=ProjectResultStatus.FAILED,
            summary="Project execution failed",
            error="backend unavailable",
        )
        runtime = MVPApplicationRuntime(
            observer=MockObserver(
                [Observation(content="do the work", source="human")]
            ),
            knowledge=knowledge,
            planner=MockProjectPlanner(
                [ProjectProposal(title="Work", goal="do the work", reason="requested")]
            ),
            approval=FixedApproval(True),
            project_manager=manager,
            executor=MockProjectExecutor(executor_result),
        )

        report = runtime.run()

        project = report.projects[0]
        result = report.results[0]
        stored_project = manager.get(project.id)
        stored_knowledge = knowledge.get(f"result-{project.id}")
        assert project.status is ProjectStatus.FAILED
        assert result.status is ProjectResultStatus.FAILED
        assert stored_project.result.status is ProjectResultStatus.FAILED
        assert stored_project.result.error == result.error == "backend unavailable"
        assert stored_knowledge.content["status"] == "failed"
        assert stored_knowledge.content["error"] == "backend unavailable"
    finally:
        db.close()
