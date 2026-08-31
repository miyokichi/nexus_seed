"""Human request -> Semantica Knowledge -> existing bounded NEXUS loop."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from nexus_seed.app.flows import ClosedLoopMVPApplication, ClosedLoopRequest
from nexus_seed.modules.knowledge import (
    ExistingKnowledgeGateway,
    KnowledgeLedger,
    SemanticaKnowledgeAdapter,
    load_ontology_yaml,
)
from nexus_seed.modules.knowledge.adapters.sqlite import KnowledgeStore
from nexus_seed.modules.knowledge.projection import WorldStateProjection
from nexus_seed.modules.observer import TextObserver
from nexus_seed.modules.planner import RequiredDeliverablePlanner
from nexus_seed.modules.project_manager import (
    A2AAgentRuntime,
    A2AMessage,
    A2AMessageType,
    Dispatch,
    ProjectOrchestrator,
    ProjectStatus,
)
from nexus_seed.modules.project_manager.workspace import GrantPolicy
from nexus_seed.platform.contracts.mvp import KnowledgeItem, ProjectProposal
from nexus_seed.policy.approval.mvp import FixedApproval
from nexus_seed.resources.scope import ResourceScope
from nexus_seed.storage import Database


ROOT = Path(__file__).parents[1]
SAMPLE = ROOT / "modules" / "knowledge" / "samples" / "assumption_v0.1.yaml"
ONTOLOGY = ROOT / "modules" / "knowledge" / "samples" / "ontology_v0.1.yaml"


class CanonicalGraphRuntime:
    """Faithfully records the already-validated Canonical mapping for this E2E."""

    def build(self, source, *, content, infer_relations, relation_types):
        return {
            "entities": list(source["entities"]),
            "relationships": list(source["relationships"]),
            "metadata": {
                "num_entities": len(source["entities"]),
                "num_relationships": len(source["relationships"]),
            },
        }


class _RecordingPlanner(RequiredDeliverablePlanner):
    """The ordinary Planner, keeping what it was given so the test can read it."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[KnowledgeItem] = []

    def propose(self, context: KnowledgeItem) -> list[ProjectProposal]:
        self.contexts.append(context)
        return super().propose(context)


class ArtifactAgentTransport:
    """Existing A2A boundary fixture; execution derives its output from the goal."""

    def __init__(self) -> None:
        self.sent = []

    async def open(self, config):
        return "a2a://little-agent-fixture"

    async def start(self, config, envelope):
        self.sent.append((config, envelope))
        workspace = Path(config.workspace)
        artifact = workspace / "performance_report.txt"
        artifact.write_text(
            "GenX WL Width: typical=50 nm, variation=10 nm.\n",
            encoding="utf-8",
        )
        return Dispatch(
            messages=[
                A2AMessage(
                    type=A2AMessageType.PROJECT_COMPLETED,
                    project_id=config.project_id,
                    source_agent_id=config.agent_id,
                    payload={
                        "summary": "Created the required performance report from Knowledge.",
                        "artifacts": ["performance_report.txt"],
                        "world_facts": [
                            {
                                "entity": "performance_report",
                                "attribute": "status",
                                "value": "created",
                            }
                        ],
                    },
                )
            ]
        )

    async def collect(self, _config, _handle):
        return None

    async def abandon(self, _handle):
        return True

    async def close(self, _agent_id):
        return None

    async def alive(self):
        return True


async def _run_human_request_e2e(tmp_path, *, runtime) -> None:
    original = tmp_path / "resources" / "assumption_sample.txt"
    original.parent.mkdir()
    original.write_text(
        "GenX uses WL Width. Performance evaluation report is missing.\n",
        encoding="utf-8",
    )
    adapter_options = {"runtime": runtime} if runtime is not None else {}
    semantic = SemanticaKnowledgeAdapter(
        tmp_path / "semantica.json",
        ontology=load_ontology_yaml(ONTOLOGY),
        **adapter_options,
    )
    semantic.ingest(SAMPLE)

    database = Database(tmp_path / "nexus.db")
    ledger = KnowledgeLedger(KnowledgeStore(database))
    knowledge = ExistingKnowledgeGateway(ledger, semantic_backend=semantic)
    planner = _RecordingPlanner()
    transport = ArtifactAgentTransport()
    orchestrator = ProjectOrchestrator(
        database,
        agent_runtime=A2AAgentRuntime(transport),
        workspace_root=str(tmp_path / "workspaces"),
        grant_policy=GrantPolicy(scope=ResourceScope.read_only(original.parent)),
    )
    application = ClosedLoopMVPApplication(
        observer=TextObserver(
            "GenXのWL Widthについて現在分かっていることを確認し、必要な作業を進めて。",
            source="human",
        ),
        knowledge=knowledge,
        planner=planner,
        approval=FixedApproval(True),
        project_manager=orchestrator,
    )
    try:
        report = await application.run_until_stable(
            ClosedLoopRequest(
                goal="GenXのWL Widthについて必要な成果物を完成させる。",
                resource_uris=(f"file:{original}",),
            ),
            max_iterations=3,
        )

        assert report.status == "stable"
        assert report.iterations == 2
        assert report.stop_reason == "no_action"
        assert [run.state for run in report.runs] == ["COMPLETED", "NO_ACTION"]
        project = report.runs[0].project
        assert project is not None and project.status is ProjectStatus.COMPLETED
        assert (tmp_path / "workspaces" / project.id / "performance_report.txt").is_file()
        semantic_item = next(
            item
            for item in planner.contexts[0].content["relevant_knowledge"]
            if item["source"] == "semantica"
        )
        assert semantic_item["content"]["properties"]["wl_width"] == {
            "typical": 50,
            "variation": 10,
            "unit": "nm",
        }
        assert semantic_item["content"]["relations"]["explicit"][0]["type"] == (
            "uses_parameter"
        )
        assert semantic_item["content"]["sources"]
        assert report.last_world_diff.changes[0].new_value == "created"
        assert WorldStateProjection(ledger).view().get(
            "performance_report", "status"
        ) == "created"
        assert report.runs[0].result_observation is not None
        assert len(transport.sent) == 1
    finally:
        orchestrator.close()
        database.close()


async def test_human_request_uses_semantica_and_runs_until_no_action(tmp_path) -> None:
    await _run_human_request_e2e(tmp_path, runtime=CanonicalGraphRuntime())


@pytest.mark.skipif(
    importlib.util.find_spec("semantica") is None,
    reason="install the semantica extra for the real-library Full E2E",
)
async def test_real_semantica_human_request_runs_until_no_action(tmp_path) -> None:
    await _run_human_request_e2e(tmp_path, runtime=None)
