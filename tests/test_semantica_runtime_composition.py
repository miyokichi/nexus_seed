"""Configuration alone attaches a Semantica Knowledge backend to the Runtime.

Nothing here composes an adapter by hand: every case starts from environment
settings, exactly as an operator's ``.env`` would.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from nexus_seed.app import AppSettings, build_runtime
from nexus_seed.integrations.semantica_config import (
    SemanticaConfigurationError,
    SemanticaSettings,
    build_knowledge_gateway,
    build_semantic_backend,
)
from nexus_seed.modules.knowledge import (
    KnowledgeLedger,
    SemanticaKnowledgeAdapter,
    load_ontology_yaml,
)
from nexus_seed.modules.knowledge.adapters.sqlite import KnowledgeStore
from nexus_seed.modules.planner import RequiredDeliverablePlanner
from nexus_seed.platform.contracts.mvp import KnowledgeItem
from nexus_seed.storage import Database


ROOT = Path(__file__).parents[1]
SAMPLE = ROOT / "modules" / "knowledge" / "samples" / "assumption_v0.1.yaml"
ONTOLOGY = ROOT / "modules" / "knowledge" / "samples" / "ontology_v0.1.yaml"

ENV_VARS = (
    "NEXUS_SEED_SEMANTICA_SNAPSHOT",
    "NEXUS_SEED_SEMANTICA_ONTOLOGY",
    "NEXUS_SEED_SEMANTICA_INFER_RELATIONS",
    "NEXUS_SEED_DATA_DIR",
    "NEXUS_SEED_LLM_ENABLED",
    "NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED",
)


class CanonicalGraphRuntime:
    """Record the already-validated Canonical mapping without Semantica."""

    def build(self, source, *, content, infer_relations, relation_types):
        return {
            "entities": list(source["entities"]),
            "relationships": list(source["relationships"]),
            "metadata": {},
        }


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


#: Ingest uses the real library when the optional extra is installed, and the
#: deterministic Canonical runtime otherwise, so these cases stay runnable in
#: an environment that deliberately does not have Semantica.
SEMANTICA_INSTALLED = importlib.util.find_spec("semantica") is not None


def _snapshot(tmp_path: Path) -> Path:
    """Ingest the Canonical sample and return the persisted snapshot path."""

    path = tmp_path / "semantica-knowledge.json"
    options = {} if SEMANTICA_INSTALLED else {"runtime": CanonicalGraphRuntime()}
    SemanticaKnowledgeAdapter(
        path, ontology=load_ontology_yaml(ONTOLOGY), **options
    ).ingest(SAMPLE)
    return path


def _env(tmp_path: Path, **values: str) -> str:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()),
        encoding="utf-8",
    )
    return str(path)


def test_no_semantica_settings_starts_the_ordinary_runtime(tmp_path):
    """Semantica配置なし: the application starts and Knowledge stays lexical."""

    env_file = _env(
        tmp_path,
        NEXUS_SEED_DATA_DIR=str(tmp_path / "data"),
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED="true",
    )
    settings = SemanticaSettings.from_env(env_file)

    assert settings.enabled is False
    assert build_semantic_backend(settings) is None

    runtime = build_runtime(AppSettings.from_env(env_file), env_file=env_file)
    try:
        assert runtime.semantic_knowledge is None
        assert runtime.knowledge_loop is not None
        assert runtime.knowledge_loop.semantic_backend is None
        gateway = build_knowledge_gateway(
            KnowledgeLedger(runtime.knowledge_store), env_file=env_file
        )
        assert gateway.semantic_backend is None
        assert gateway.search("WL Width") == []
    finally:
        runtime.close()


def test_configured_snapshot_attaches_the_adapter_to_the_runtime(tmp_path):
    """Semantica配置あり: startup builds the adapter and injects it."""

    snapshot = _snapshot(tmp_path)
    env_file = _env(
        tmp_path,
        NEXUS_SEED_DATA_DIR=str(tmp_path / "data"),
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_SEMANTICA_SNAPSHOT=str(snapshot),
        NEXUS_SEED_SEMANTICA_ONTOLOGY=str(ONTOLOGY),
    )
    settings = SemanticaSettings.from_env(env_file)

    assert settings.enabled is True
    assert settings.snapshot_path == snapshot.resolve()
    assert settings.ontology_path == ONTOLOGY.resolve()

    runtime = build_runtime(AppSettings.from_env(env_file), env_file=env_file)
    try:
        backend = runtime.semantic_knowledge
        assert isinstance(backend, SemanticaKnowledgeAdapter)
        assert backend.snapshot_path == snapshot.resolve()
        assert backend.ontology is not None
        # The loop received the same backend; it queries through the Knowledge
        # contract and never learns which backend answered.
        assert runtime.knowledge_loop.semantic_backend is backend
    finally:
        runtime.close()


def test_snapshot_is_loaded_and_answers_a_knowledge_query(tmp_path):
    """A persisted snapshot survives a fresh adapter and still answers."""

    snapshot = _snapshot(tmp_path)
    assert json.loads(snapshot.read_text(encoding="utf-8"))["documents"]

    env_file = _env(tmp_path, NEXUS_SEED_SEMANTICA_SNAPSHOT=str(snapshot))
    backend = build_semantic_backend(env_file=env_file)
    assert backend is not None

    context = backend.query("WL Width")
    assert context is not None
    payload = context.to_dict()
    assert payload["properties"]["wl_width"] == {
        "typical": 50,
        "variation": 10,
        "unit": "nm",
    }
    assert [relation["type"] for relation in payload["relations"]["explicit"]] == [
        "uses_parameter"
    ]
    assert payload["sources"]


def test_configured_gateway_gives_the_planner_retrievable_knowledge(tmp_path):
    """Planner retrieval: the Gateway hands over normalized semantic context."""

    snapshot = _snapshot(tmp_path)
    env_file = _env(
        tmp_path,
        NEXUS_SEED_SEMANTICA_SNAPSHOT=str(snapshot),
        NEXUS_SEED_SEMANTICA_ONTOLOGY=str(ONTOLOGY),
    )
    database = Database(tmp_path / "nexus.db")
    try:
        gateway = build_knowledge_gateway(
            KnowledgeLedger(KnowledgeStore(database)), env_file=env_file
        )
        retrieved = gateway.search("GenXのWL Widthと必要な成果物")

        assert retrieved, "the configured backend must answer a goal query"
        planning_context = KnowledgeItem(
            id="planning-1",
            source="nexus_seed_planning_context",
            content={
                "goal": "GenXのWL Widthについて必要な成果物を完成させる。",
                "observation": {"id": "obs-1", "content": "human request"},
                "relevant_knowledge": [
                    {
                        "id": item.id,
                        "content": item.content,
                        "source": item.source,
                        "metadata": dict(item.metadata),
                    }
                    for item in retrieved
                ],
            },
        )
        [proposal] = RequiredDeliverablePlanner().propose(planning_context)

        assert proposal.context == {"deliverable_id": "performance_report"}
        assert "performance_report.txt" in proposal.goal
    finally:
        database.close()


def test_runtime_starts_without_the_optional_semantica_package(tmp_path, monkeypatch):
    """Semantica未install + 設定なし: importing it is never attempted."""

    _block_semantica_import(monkeypatch)
    with pytest.raises(ImportError):
        importlib.import_module("semantica")

    env_file = _env(
        tmp_path,
        NEXUS_SEED_DATA_DIR=str(tmp_path / "data"),
        NEXUS_SEED_LLM_ENABLED="false",
    )
    runtime = build_runtime(AppSettings.from_env(env_file), env_file=env_file)
    try:
        assert runtime.semantic_knowledge is None
        assert runtime.knowledge_loop is not None
    finally:
        runtime.close()


def test_configured_query_works_without_the_optional_semantica_package(
    tmp_path, monkeypatch
):
    """Reading an already-ingested snapshot needs no Semantica install."""

    snapshot = _snapshot(tmp_path)
    _block_semantica_import(monkeypatch)
    env_file = _env(tmp_path, NEXUS_SEED_SEMANTICA_SNAPSHOT=str(snapshot))

    backend = build_semantic_backend(env_file=env_file)
    assert backend is not None
    assert backend.query("performance report") is not None


def test_ontology_without_a_snapshot_is_refused(tmp_path):
    """A half-configured backend is a startup error, not a silent no-op."""

    env_file = _env(tmp_path, NEXUS_SEED_SEMANTICA_ONTOLOGY=str(ONTOLOGY))
    with pytest.raises(SemanticaConfigurationError):
        SemanticaSettings.from_env(env_file)


def test_missing_ontology_file_is_refused(tmp_path):
    env_file = _env(
        tmp_path,
        NEXUS_SEED_SEMANTICA_SNAPSHOT=str(tmp_path / "snapshot.json"),
        NEXUS_SEED_SEMANTICA_ONTOLOGY=str(tmp_path / "absent.yaml"),
    )
    with pytest.raises(SemanticaConfigurationError):
        SemanticaSettings.from_env(env_file)


def test_snapshot_that_does_not_exist_yet_is_allowed(tmp_path):
    """The file appears at the first ingest; until then queries find nothing."""

    env_file = _env(
        tmp_path, NEXUS_SEED_SEMANTICA_SNAPSHOT=str(tmp_path / "not-yet.json")
    )
    backend = build_semantic_backend(env_file=env_file)

    assert backend is not None
    assert backend.query("anything") is None


class _BlockedFinder:
    """Make ``import semantica`` fail the way an uninstalled package does."""

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy hook
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "semantica" or fullname.startswith("semantica."):
            raise ImportError(f"No module named {fullname!r}")
        return None


def _block_semantica_import(monkeypatch) -> None:
    for name in [key for key in sys.modules if key.split(".")[0] == "semantica"]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder(), *sys.meta_path])
    importlib.invalidate_caches()


async def test_the_configured_backend_reaches_the_running_loops_assessment(tmp_path):
    """The daemon's evaluator sees retrieved Knowledge beside its new evidence."""

    from nexus_seed.modules.project_manager import (
        InProcessAgentRuntime,
        ProjectOrchestrator,
    )

    snapshot = _snapshot(tmp_path)
    env_file = _env(
        tmp_path,
        NEXUS_SEED_DATA_DIR=str(tmp_path / "data"),
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_SEMANTICA_SNAPSHOT=str(snapshot),
    )
    runtime = build_runtime(AppSettings.from_env(env_file), env_file=env_file)
    try:
        evaluator = _RecordingBackend()
        loop = runtime.knowledge_loop
        loop.backend = evaluator
        loop.orchestrator = ProjectOrchestrator(
            runtime.db, agent_runtime=InProcessAgentRuntime()
        )
        try:
            await loop.record_manual(
                "GenXのWL Widthの評価報告書が必要です。",
                source_event_key="manual-genx-v1",
            )
        finally:
            loop.orchestrator.close()

        [request] = evaluator.requests
        semantic = request.context["semantic_knowledge"]
        assert semantic["properties"]["wl_width"]["typical"] == 50
        assert semantic["sources"]
        # The evaluator is told what is known, never which backend knew it.
        assert "semantica" not in json.dumps(semantic).casefold()
    finally:
        runtime.close()


class _RecordingBackend:
    """Answer every assessment with "nothing to propose", and keep the request."""

    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        from nexus_seed.backends import proposal_response

        self.requests.append(request)
        return proposal_response({"proposals": []})
