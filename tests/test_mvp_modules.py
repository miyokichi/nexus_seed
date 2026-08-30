"""Unit coverage for each independently replaceable MVP module."""

from __future__ import annotations

import pytest

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge import KnowledgeLedger
from nexus_seed.mvp import (
    DefaultProjectExecutor,
    CLIHumanApproval,
    ExistingBackendLLMProvider,
    ExistingKnowledgeGateway,
    ManualIngressObserver,
    ExistingProjectManagerAdapter,
    KnowledgeItem,
    Observation,
    ProjectExecutionRequest,
    ProjectProposal,
    ProjectResult,
    ProjectResultStatus,
    ProjectStatus,
    SimpleProjectPlanner,
    TextObserver,
    to_knowledge_item,
)
from nexus_seed.mvp.interfaces import (
    KnowledgeGateway,
    LLMProvider,
    Observer,
    ProjectExecutor,
    ProjectManager,
    ProjectPlanner,
)
from nexus_seed.mvp.testing import MockLLMProvider
from nexus_seed.orchestrator.project_manager import ProjectManager as DurableProjectManager
from nexus_seed.storage import Database, KnowledgeStore
from nexus_seed.storage.orchestrator_store import ProjectStore
from nexus_seed.runtime import Runtime
from nexus_seed.core.process import ProcessDefinition


def test_text_observer_is_independent_and_one_shot():
    observer = TextObserver(["first", "", "second"], source="cli")

    observations = observer.observe()

    assert isinstance(observer, Observer)
    assert [item.content for item in observations] == ["first", "second"]
    assert all(item.source == "cli" for item in observations)
    assert observer.observe() == []


def test_cli_human_approval_requires_an_explicit_yes():
    output = []
    approved = CLIHumanApproval(
        input_fn=lambda _prompt: "yes",
        output_fn=output.append,
    )
    rejected = CLIHumanApproval(
        input_fn=lambda _prompt: "",
        output_fn=lambda _line: None,
    )
    proposal = ProjectProposal(title="README改善", goal="improve", reason="request")

    assert approved.approve(proposal) is True
    assert rejected.approve(proposal) is False
    assert output == [
        "Project Proposal",
        "Title: README改善",
        "Goal: improve",
        "Reason: request",
    ]


def test_manual_ingress_observer_reuses_ingress_and_deduplicates(tmp_path):
    runtime = Runtime(tmp_path / "ingress.db")
    try:
        handled = []

        async def handler(ctx):
            handled.append(str(ctx.event.id))
            return ctx.complete()

        runtime.register_process(
            ProcessDefinition(
                name="must_not_run_from_observer",
                version="1",
                handler="must_not_run_from_observer",
                trigger_event_types=("mvp_human_input",),
            ),
            handler,
        )
        first = ManualIngressObserver(
            runtime.ingress,
            "Improve README",
            source_event_key="request-1",
        )
        duplicate = ManualIngressObserver(
            runtime.ingress,
            "Improve README",
            source_event_key="request-1",
        )

        observations = first.observe()

        assert len(observations) == 1
        assert observations[0].content == "Improve README"
        assert observations[0].metadata["source_event_key"] == "request-1"
        assert duplicate.observe() == []
        assert len(runtime.event_store.all()) == 1
        assert runtime.process_store.all_instances() == []
        assert handled == []
        assert runtime.get_pending_event_delivery_count() == 1
    finally:
        runtime.close()


def test_existing_knowledge_gateway_put_get_search_and_revision(tmp_path):
    db = Database(tmp_path / "mvp.db")
    try:
        gateway = ExistingKnowledgeGateway(KnowledgeLedger(KnowledgeStore(db)))
        original = KnowledgeItem(
            id="knowledge-1",
            content="README needs examples",
            source="human",
            metadata={"language": "en"},
        )

        assert gateway.put(original) is None
        gateway.put(original)  # identical put is idempotent
        revised = KnowledgeItem(
            id=original.id,
            content="README needs a quick start",
            source="human",
            metadata={"language": "en"},
        )
        gateway.put(revised)

        assert isinstance(gateway, KnowledgeGateway)
        assert gateway.get("knowledge-1") == revised
        assert [item.id for item in gateway.search("quick start")] == ["knowledge-1"]
        assert len(gateway.ledger.history("knowledge-1")) == 2
    finally:
        db.close()


def test_simple_planner_proposes_without_creating_a_project():
    planner = SimpleProjectPlanner()

    proposals = planner.propose(
        to_knowledge_item(
            Observation(content="NEXUS SEED の README を改善する。", source="human")
        )
    )

    assert isinstance(planner, ProjectPlanner)
    assert len(proposals) == 1
    assert proposals[0].title == "README改善"
    assert proposals[0].goal == "READMEを確認し、改善案を作成する"


def test_common_models_normalize_observations_and_results_for_knowledge():
    observation = Observation(
        id="observation-1",
        content="Improve README",
        source="human",
        metadata={"channel": "cli"},
    )
    result = ProjectResult(
        project_id="project-1",
        status=ProjectResultStatus.FAILED,
        summary="Execution failed",
        outputs={"attempted": True},
        error="backend unavailable",
    )

    observation_item = to_knowledge_item(observation)
    result_item = to_knowledge_item(result)

    assert observation_item == KnowledgeItem(
        id=observation.id,
        content=observation.content,
        source=observation.source,
        created_at=observation.observed_at,
        metadata=observation.metadata,
    )
    assert result_item.id == "result-project-1"
    assert result_item.source == "project_executor"
    assert result_item.content == {
        "project_id": "project-1",
        "status": "failed",
        "summary": "Execution failed",
        "outputs": {"attempted": True},
        "error": "backend unavailable",
    }


def test_existing_project_manager_adapter_owns_only_lifecycle(tmp_path):
    db = Database(tmp_path / "mvp.db")
    try:
        manager = ExistingProjectManagerAdapter(
            DurableProjectManager(ProjectStore(db))
        )
        proposal = ProjectProposal(
            title="README改善",
            goal="READMEを確認し、改善案を作成する",
            reason="requested",
            context={"observation_id": "obs-1"},
        )

        ready = manager.create(proposal)
        running = manager.mark_running(ready.id)
        result = ProjectResult(
            project_id=ready.id,
            status=ProjectResultStatus.COMPLETED,
            summary="READMEの改善案を作成しました",
            outputs={"suggestions": ["add quick start"]},
        )
        completed = manager.mark_completed(ready.id, result)

        assert isinstance(manager, ProjectManager)
        assert ready.status is ProjectStatus.READY
        assert running.status is ProjectStatus.RUNNING
        assert completed.status is ProjectStatus.COMPLETED
        assert manager.get(ready.id).result == result
        assert manager.list() == [manager.get(ready.id)]
    finally:
        db.close()


def test_project_manager_adapter_excludes_non_mvp_blocked_projects(tmp_path):
    db = Database(tmp_path / "mvp.db")
    try:
        durable = DurableProjectManager(ProjectStore(db))
        manager = ExistingProjectManagerAdapter(durable)
        existing = durable.create("existing full-version project")
        durable.block(existing, kind="WAITING", reason="external dependency")

        assert existing.status.value == "BLOCKED"
        assert manager.get(existing.id) is None
        assert manager.list() == []

        mvp = manager.create(
            ProjectProposal(title="MVP", goal="mvp work", reason="requested")
        )
        durable.block(
            durable.get(mvp.id),
            kind="WAITING",
            reason="changed outside the MVP lifecycle",
        )
        with pytest.raises(ValueError, match="will not be represented as FAILED"):
            manager.get(mvp.id)
    finally:
        db.close()


def test_default_executor_uses_llm_and_read_only_readme_tool(tmp_path):
    (tmp_path / "README.md").write_text("# Old README\n", encoding="utf-8")
    llm = MockLLMProvider(
        default={
            "summary": "READMEの改善案を作成しました",
            "outputs": {"suggestions": ["add audience"]},
        }
    )
    executor = DefaultProjectExecutor(llm)

    result = executor.execute(
        ProjectExecutionRequest(
            project_id="project-1",
            goal="READMEを確認し、改善案を作成する",
            workspace=str(tmp_path),
        )
    )

    assert isinstance(llm, LLMProvider)
    assert isinstance(executor, ProjectExecutor)
    assert result.status is ProjectResultStatus.COMPLETED
    assert "Old README" in llm.calls[0][0][1]["content"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Old README\n"


def test_existing_backend_llm_provider_adapts_existing_interface():
    backend = FakeLLMBackend(
        default=proposal_response({"summary": "done", "outputs": {}})
    )
    provider = ExistingBackendLLMProvider(backend)

    output = provider.generate([{"role": "user", "content": "work"}], {"type": "object"})

    assert isinstance(provider, LLMProvider)
    assert output == {"summary": "done", "outputs": {}}
    assert backend.calls[0].metadata["surface"] == "mvp"
