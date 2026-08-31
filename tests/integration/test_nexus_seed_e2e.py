"""NEXUS SEED v0.1 end to end, over real process and module boundaries.

    assumption.xlsx
      -> Canonical YAML v0.1            (nexus_seed.sources.excel)
      -> Semantica snapshot             (nexus-seed-semantica ingest)
      -> Knowledge Gateway              (attached by configuration alone)
      -> Planner decision               (RequiredDeliverablePlanner)
      -> Project                        (ProjectOrchestrator)
      -> little_agent                   (a real subprocess, over real A2A)
      -> execution result -> world_facts -> Knowledge update
      -> replanning -> no_action

Nothing in the chain is stubbed out from the NEXUS side: the Semantica graph
is built by the installed library, the Project is delegated to a little_agent
process over HTTP, and that process writes the deliverable with its own
filesystem tools.  The one thing standing in for something bigger is the model
itself: a local OpenAI-compatible service answers little_agent's real HTTP LLM
client, and it answers from what the tools actually returned — it reads the
workspace manifest, follows it to the granted Canonical YAML, and reports the
values it found there.  That keeps the run deterministic without letting the
test decide anything the loop is supposed to decide.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from nexus_seed.app.flows import ClosedLoopMVPApplication, ClosedLoopRequest
from nexus_seed.integrations.project_agent_config import build_orchestrator
from nexus_seed.integrations.semantica_config import (
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
from nexus_seed.modules.knowledge.canonical import load_canonical_yaml
from nexus_seed.modules.knowledge.projection import WorldStateProjection
from nexus_seed.modules.observer import TextObserver
from nexus_seed.modules.planner import RequiredDeliverablePlanner
from nexus_seed.modules.project_manager import A2AMessageType, ProjectStatus
from nexus_seed.policy.approval.mvp import FixedApproval
from nexus_seed.sources.excel import convert_excel_to_yaml
from nexus_seed.storage import Database

from little_agent_harness import LITTLE_AGENT_PYTHON, LittleAgentProcess

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKBOOK = REPO_ROOT / "tests" / "fixtures" / "assumption.xlsx"
MAPPING = REPO_ROOT / "samples" / "excel_assumption_mapping.yaml"
ONTOLOGY = REPO_ROOT / "modules" / "knowledge" / "samples" / "ontology_v0.1.yaml"

HUMAN_REQUEST = "GenXのWL Widthについて分かっていることを確認し、必要な作業を進めて。"
GOAL = "GenXのWL Widthについて必要な成果物を完成させる。"

#: This one runs itself whenever the whole stack is actually present: an E2E
#: that has to be asked for is an E2E nobody runs.  What it needs is the two
#: optional extras and little_agent installed in its own virtual environment.
MISSING = [
    name
    for name, present in (
        ("the semantica extra", importlib.util.find_spec("semantica") is not None),
        ("the ingest extra (openpyxl)", importlib.util.find_spec("openpyxl") is not None),
        (f"little_agent at {LITTLE_AGENT_PYTHON}", LITTLE_AGENT_PYTHON.exists()),
    )
    if not present
]

requires_full_stack = pytest.mark.skipif(
    bool(MISSING), reason=f"needs {', '.join(MISSING)}"
)


#: Settings this test writes into its own ``.env``.  ``load_env_file`` never
#: overwrites an existing value, so they are cleared before and after the run:
#: otherwise one test's endpoint would silently become the next one's.
ENV_VARS = (
    "NEXUS_SEED_LLM_ENABLED",
    "NEXUS_SEED_SEMANTICA_SNAPSHOT",
    "NEXUS_SEED_SEMANTICA_ONTOLOGY",
    "NEXUS_SEED_PROJECT_AGENT_RUNTIME",
    "NEXUS_SEED_PROJECT_AGENT_URL",
    "NEXUS_SEED_PROJECT_AGENT_POLL_SECONDS",
    "NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS",
    "NEXUS_SEED_PROJECT_AGENT_REQUEST_TIMEOUT_SECONDS",
    "NEXUS_SEED_PROJECT_WORKSPACE",
    "NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS",
)


@pytest.fixture(autouse=True)
def isolated_env():
    """Start from a clean environment, and restore whatever was there."""

    saved = {name: os.environ.pop(name, None) for name in ENV_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value


class _WorkspaceReadingModel(AbstractContextManager["_WorkspaceReadingModel"]):
    """A local OpenAI-compatible service that answers from its own tool results.

    It is deterministic but not scripted against this test's expectations: the
    file it writes and the fact it reports both come from the Project goal the
    Planner wrote and from the Canonical YAML it read through little_agent's
    filesystem tools.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP hook
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.requests.append(body)
                encoded = json.dumps(owner._response(body)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> "_WorkspaceReadingModel":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _response(self, body: dict[str, Any]) -> dict[str, Any]:
        messages = body.get("messages") or []
        prompt = "\n".join(
            _text(message.get("content"))
            for message in messages
            if message.get("role") in {"system", "user"}
        )
        observed = "\n".join(
            _text(message.get("content"))
            for message in messages
            if message.get("role") == "tool"
        )
        artifact = _first(r"as\s+(\S+\.txt)\b", prompt) or "deliverable.txt"
        entity = _first(r"entity=([A-Za-z0-9_.-]+)", prompt) or "deliverable"

        if not observed:
            return _tool_call("read_file", {"path": "RESOURCES.md"})
        if "schema_version" not in observed:
            canonical = _first(r"`([^`]*\.yaml)`", observed)
            if canonical:
                return _tool_call("read_file", {"path": canonical})
        if "[write_file]" not in observed:
            facts = _first(r"^(.*\(Parameter\):.*)$", observed) or "no parameter found"
            return _tool_call(
                "write_file",
                {
                    "path": artifact,
                    "content": f"{facts.strip()}\nProduced from the granted Canonical YAML.\n",
                },
            )
        return _final(
            {
                "messages": [
                    {
                        "type": "PROJECT_COMPLETED",
                        "payload": {
                            "status": "completed",
                            "summary": f"Wrote {artifact} from the granted Canonical YAML.",
                            "artifacts": [artifact],
                            "world_facts": [
                                {
                                    "entity": entity,
                                    "attribute": "status",
                                    "value": "created",
                                }
                            ],
                        },
                    }
                ]
            }
        )


@requires_full_stack
async def test_excel_becomes_knowledge_and_the_loop_settles_on_no_action(tmp_path):
    documents = tmp_path / "documents"
    workspaces = tmp_path / "workspaces"
    documents.mkdir()
    workspaces.mkdir()

    # [1] Excel -> Canonical YAML.  The converter knows nothing about Knowledge.
    canonical_path = documents / "assumption.yaml"
    canonical_text = convert_excel_to_yaml(WORKBOOK, MAPPING)
    canonical_path.write_text(canonical_text, encoding="utf-8")
    canonical = load_canonical_yaml(canonical_path)
    assert canonical.source_file == "assumption.xlsx"
    assert {entity.type for entity in canonical.entities} == {
        "Generation",
        "Parameter",
        "Deliverable",
    }

    # [2] Canonical YAML -> Semantica, through the Knowledge module's adapter.
    snapshot = tmp_path / "semantica-knowledge.json"
    graph = SemanticaKnowledgeAdapter(
        snapshot, ontology=load_ontology_yaml(ONTOLOGY)
    ).ingest(canonical_path)
    assert graph["entities"]

    # [3] Persistence and reload: a new adapter over the same snapshot answers
    # without re-ingesting anything.
    reloaded = SemanticaKnowledgeAdapter(snapshot)
    assert reloaded.query("WL Width") is not None

    with _WorkspaceReadingModel() as model, LittleAgentProcess(
        llm_url=model.base_url,
        workspace_root=workspaces,
        readable_root=documents,
        temp_root=tmp_path,
    ) as little:
        env_file = _env_file(
            tmp_path,
            snapshot=snapshot,
            workspaces=workspaces,
            readable_root=documents,
            agent_url=little.url,
        )
        assert SemanticaSettings.from_env(env_file).snapshot_path == snapshot.resolve()
        assert build_semantic_backend(env_file=env_file) is not None
        database = Database(tmp_path / "nexus.db")
        ledger = KnowledgeLedger(KnowledgeStore(database))
        # [4] The whole runtime is composed from settings, not by hand.
        knowledge = build_knowledge_gateway(ledger, env_file=env_file)
        assert knowledge.semantic_backend is not None
        orchestrator = build_orchestrator(database, env_file=env_file)
        planner = RequiredDeliverablePlanner()
        application = ClosedLoopMVPApplication(
            observer=TextObserver(HUMAN_REQUEST, source="human"),
            knowledge=knowledge,
            planner=planner,
            approval=FixedApproval(True),
            project_manager=orchestrator,
            project_settle_timeout=60.0,
            project_settle_interval=0.1,
        )
        try:
            report = await application.run_until_stable(
                ClosedLoopRequest(
                    goal=GOAL,
                    resource_uris=(f"file:{canonical_path}",),
                ),
                max_iterations=3,
            )

            # [12] The loop stops because there is nothing left to do.
            assert report.status == "stable"
            assert report.stop_reason == "no_action"
            assert report.iterations == 2
            assert [run.state for run in report.runs] == ["COMPLETED", "NO_ACTION"]

            # [5] The Planner decided from retrieved Knowledge, not from the text.
            first = report.runs[0]
            semantic = next(
                item
                for item in first.planning_context.relevant_knowledge
                if isinstance(item.content, dict) and item.content.get("entities")
            )
            assert semantic.content["properties"]["wl_width"] == {
                "typical": {"value": 50, "unit": "nm"},
                "variation": {"value": 10, "unit": "nm"},
            }
            assert first.proposal.context == {"deliverable_id": "performance_report"}

            # [6][7] Project Manager delegated it over A2A to the real process.
            project = first.project
            assert project is not None
            assert project.status is ProjectStatus.COMPLETED
            assert project.assigned_agent_id
            [completed] = [
                message
                for direction, message in orchestrator.gateway.history(project.id)
                if direction == "inbound"
                and message.type is A2AMessageType.PROJECT_COMPLETED
            ]

            # [8] little_agent's own process wrote the deliverable.
            artifact = workspaces / project.id / "performance_report.txt"
            assert artifact.is_file()
            assert "typical = 50 nm" in artifact.read_text(encoding="utf-8")

            # [9] The result came back as world_facts on the A2A channel.
            assert completed.payload["world_facts"] == [
                {"entity": "performance_report", "attribute": "status", "value": "created"}
            ]
            assert first.result_observation is not None

            # [10] Knowledge, and the World View derived from it, moved.
            assert report.last_world_diff.changes[0].entity == "performance_report"
            assert report.last_world_diff.changes[0].new_value == "created"
            assert (
                WorldStateProjection(ledger).view().get("performance_report", "status")
                == "created"
            )

            # [11] Replanning ran on the world change and proposed nothing.
            second = report.runs[1]
            assert second.observation.source == "knowledge_world_diff"
            assert second.proposal is None
        finally:
            orchestrator.close()
            database.close()

        # The granted document was read where it lives and left untouched, and
        # the model saw the workbook's numbers only because a real tool call
        # returned them.
        assert canonical_path.read_text(encoding="utf-8") == canonical_text
        transcript = json.dumps(model.requests)
        assert "RESOURCES.md" in transcript
        assert "schema_version" in transcript
        little.stop()
        assert "execution completed" in little.output


def _env_file(
    tmp_path: Path,
    *,
    snapshot: Path,
    workspaces: Path,
    readable_root: Path,
    agent_url: str,
) -> str:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "NEXUS_SEED_LLM_ENABLED=false",
                f"NEXUS_SEED_SEMANTICA_SNAPSHOT={snapshot}",
                f"NEXUS_SEED_SEMANTICA_ONTOLOGY={ONTOLOGY}",
                "NEXUS_SEED_PROJECT_AGENT_RUNTIME=a2a",
                f"NEXUS_SEED_PROJECT_AGENT_URL={agent_url}",
                "NEXUS_SEED_PROJECT_AGENT_POLL_SECONDS=0.05",
                "NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS=60",
                "NEXUS_SEED_PROJECT_AGENT_REQUEST_TIMEOUT_SECONDS=10",
                f"NEXUS_SEED_PROJECT_WORKSPACE={workspaces}",
                f"NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS={readable_root}",
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _completion(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{name}-{_digest(arguments)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        finish_reason="tool_calls",
    )


def _digest(arguments: dict[str, Any]) -> str:
    """A stable id for one tool call, so a rerun sends the same conversation."""

    encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:8]


def _final(payload: dict[str, Any]) -> dict[str, Any]:
    return _completion(
        {"role": "assistant", "content": json.dumps(payload)}, finish_reason="stop"
    )


def _completion(message: dict[str, Any], *, finish_reason: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-e2e",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict)
        )
    return ""


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else None
