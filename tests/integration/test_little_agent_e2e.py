"""Real-process E2E between NEXUS SEED and the in-repository little_agent.

The Agent Runtime and A2A transport are real.  A small local OpenAI-compatible
service makes the model choices deterministic while still exercising
little_agent's HTTP LLM client, Agent loop, filesystem tools, schema validator,
and A2A DataPart response.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from nexus_seed.integrations.a2a import A2AEndpoint
from nexus_seed.integrations.project_agent import A2AProjectAgentTransport
from nexus_seed.modules.project_manager import (
    A2AAgentRuntime,
    A2AMessageType,
    AgentStatus,
    ProjectOrchestrator,
    ProjectStatus,
    RoutingAction,
)
from nexus_seed.modules.project_manager.workspace.models import ResourceGrant, with_grant
from nexus_seed.modules.project_manager.workspace.policy import GrantPolicy
from nexus_seed.resources.scope import ResourceScope

from little_agent_harness import (
    LITTLE_AGENT_PYTHON,
    LittleAgentProcess,
    free_port,
)

pytestmark = pytest.mark.integration

RUN_REAL_E2E = (os.environ.get("RUN_LITTLE_AGENT_E2E") or "").strip().lower() in {
    "1",
    "true",
    "yes",
}


class _ScriptedLLM(AbstractContextManager["_ScriptedLLM"]):
    """OpenAI-compatible HTTP service choosing a fixed, observable tool sequence."""

    def __init__(self, original: Path) -> None:
        self.original = original
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP hook
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.requests.append(body)
                messages = body.get("messages") or []
                tool_results = [item for item in messages if item.get("role") == "tool"]
                response = owner._response(len(tool_results))
                encoded = json.dumps(response).encode("utf-8")
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

    def __enter__(self) -> "_ScriptedLLM":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _response(self, completed_tools: int) -> dict[str, Any]:
        calls = [
            ("read_file", {"path": str(self.original)}),
            (
                "write_file",
                {"path": str(self.original), "content": "must not replace original"},
            ),
            ("write_file", {"path": "../outside.txt", "content": "must not escape"}),
            (
                "write_file",
                {"path": "editable.txt", "content": "workspace is writable\n"},
            ),
            (
                "write_file",
                {"path": "result.txt", "content": "Summary: alpha beta gamma\n"},
            ),
        ]
        if completed_tools < len(calls):
            name, arguments = calls[completed_tools]
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{completed_tools + 1}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "messages": [
                            {
                                "type": "PROJECT_COMPLETED",
                                "payload": {
                                    "status": "completed",
                                    "summary": "Read input.txt and wrote its short summary.",
                                    "output_file": "result.txt",
                                },
                            }
                        ]
                    }
                ),
            }
            finish_reason = "stop"
        return {
            "id": f"chatcmpl-{completed_tools}",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def _orchestrator(db_path: Path, endpoint: str, workspace_root: Path) -> ProjectOrchestrator:
    original_root = db_path.parent / "original"
    original_root.mkdir(exist_ok=True)
    transport = A2AProjectAgentTransport(
        A2AEndpoint(
            url=endpoint,
            poll_interval_seconds=0.05,
            timeout_seconds=20,
            request_timeout_seconds=5,
        )
    )
    return ProjectOrchestrator(
        db_path,
        agent_runtime=A2AAgentRuntime(transport),
        workspace_root=str(workspace_root),
        grant_policy=GrantPolicy(
            scope=ResourceScope(read_roots=[original_root], write_roots=[])
        ),
    )


@pytest.mark.skipif(
    not RUN_REAL_E2E or not LITTLE_AGENT_PYTHON.exists(),
    reason="set RUN_LITTLE_AGENT_E2E=1 after installing modules/little_agent",
)
async def test_nexus_executes_one_project_through_real_little_agent_process(tmp_path):
    original_root = tmp_path / "original"
    workspace_root = tmp_path / "workspaces"
    original_root.mkdir()
    workspace_root.mkdir()
    original = original_root / "input.txt"
    original_text = "alpha beta gamma\n"
    original.write_text(original_text, encoding="utf-8")

    with _ScriptedLLM(original) as llm, LittleAgentProcess(
        llm_url=llm.base_url,
        workspace_root=workspace_root,
        readable_root=original_root,
        temp_root=tmp_path,
    ) as little:
        orchestrator = _orchestrator(tmp_path / "projects.db", little.url, workspace_root)
        try:
            context = with_grant(
                {},
                ResourceGrant(
                    uri=f"file:{original}",
                    reason="input for the summary",
                ),
            )
            decision, project = await orchestrator.submit(
                "Read the supplied input file and write a short summary to result.txt.",
                source="e2e-test",
                project_context=context,
            )

            assert decision.action is RoutingAction.CREATE_PROJECT
            assert project is not None
            assert project.assigned_agent_id
            assert project.status is ProjectStatus.COMPLETED
            assert project.summary == "Read input.txt and wrote its short summary."

            workspace = workspace_root / project.id
            assert (workspace / "editable.txt").read_text(encoding="utf-8") == (
                "workspace is writable\n"
            )
            assert (workspace / "result.txt").read_text(encoding="utf-8") == (
                "Summary: alpha beta gamma\n"
            )
            assert original.read_text(encoding="utf-8") == original_text
            assert not (workspace_root / "outside.txt").exists()

            inbound = [
                message
                for direction, message in orchestrator.gateway.history(project.id)
                if direction == "inbound"
            ]
            [completed] = [
                message
                for message in inbound
                if message.type is A2AMessageType.PROJECT_COMPLETED
            ]
            assert completed.payload == {
                "status": "completed",
                "summary": "Read input.txt and wrote its short summary.",
                "output_file": "result.txt",
            }
            [agent] = orchestrator.agent_store.for_project(project.id)
            assert agent.status is AgentStatus.IDLE
        finally:
            orchestrator.close()

        # The actual tool results went back through the LLM HTTP boundary.
        transcript = json.dumps(llm.requests)
        assert original_text.strip() in transcript
        assert transcript.count("outside allowed write paths") >= 2
        little.stop()
        assert "request received" in little.output
        assert "workspace granted" in little.output
        assert "execution started" in little.output
        assert "execution completed" in little.output
        assert "result ready" in little.output
        assert "result sent" in little.output


async def test_unreachable_little_agent_never_completes_project_and_records_error(tmp_path):
    endpoint = f"http://127.0.0.1:{free_port()}"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    orchestrator = _orchestrator(tmp_path / "projects.db", endpoint, workspace_root)
    try:
        project = await orchestrator.start_project("This must remain retryable.")

        assert project.status is ProjectStatus.CREATED
        assert project.is_live
        assert project.assigned_agent_id is None
        [failed_agent] = orchestrator.agent_store.for_project(project.id)
        assert failed_agent.status is AgentStatus.FAILED
        assert failed_agent.metadata.get("error")
    finally:
        orchestrator.close()
