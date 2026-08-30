"""The Cockpit half of the context path: see the notes, judge the suggestions.

The decision logic is tested in tests/test_context_assessment.py; this covers
what an operator can actually see and press.
"""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.cockpit import CockpitService
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.knowledge.autonomous_loop import KnowledgeLoop
from nexus_seed.knowledge.context_assessment import (
    CANDIDATE_PENDING_REVIEW,
    CANDIDATE_REJECTED,
    CANDIDATE_ROUTED,
)
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.runtime.runtime import Runtime

TOKEN = "cockpit-token"
TASK = "Project Aのblockerを調査する"


class Backend(FakeLLMBackend):
    async def execute(self, request):
        self.calls.append(request)
        if request.metadata.get("kind") == "context_assessment":
            return proposal_response(
                {
                    "terminology_issues": [
                        {"description": "「放置」の意味が定義されていない", "confidence": 0.4}
                    ],
                    "contradictions": [],
                    "goal_gaps": [
                        {"description": "BLOCKED Projectが存在する", "confidence": 0.9}
                    ],
                    "unknowns": [],
                    "task_candidates": [
                        {
                            "description": TASK,
                            "reason": "goals.mdに反している",
                            "evidence": ["situation.md"],
                            "confidence": 0.8,
                            "suggested_assignee_type": "AGENT",
                        }
                    ],
                }
            )
        return proposal_response({"proposals": []})


async def assessed(tmp_path):
    runtime = Runtime(tmp_path / "app.db")
    orchestrator = ProjectOrchestrator(runtime.db, agent_runtime=InProcessAgentRuntime())
    runtime.project_orchestrator = orchestrator
    root = tmp_path / "context"
    loop = KnowledgeLoop(
        runtime, orchestrator, backend=Backend(), context_root=root
    )
    runtime.knowledge_loop = loop
    loop.context_documents.ensure()
    (root / "goals.md").write_text("BLOCKED Projectを放置しない", encoding="utf-8")
    (root / "situation.md").write_text("Project Aはデータ不足でBLOCKED", encoding="utf-8")
    await loop.reconcile()
    return runtime, loop, orchestrator


def cockpit(runtime) -> CockpitService:
    return CockpitService(runtime, master_id="local-operator")


async def test_the_snapshot_shows_the_notes_the_reading_and_the_suggestions(tmp_path):
    runtime, _loop, orchestrator = await assessed(tmp_path)
    try:
        knowledge = cockpit(runtime).snapshot()["knowledge"]

        assert knowledge["context"]["goals"]["text"] == "BLOCKED Projectを放置しない"
        # Revision 1, not 2: the template `ensure` wrote was never recorded,
        # so what a person typed over it is the first thing on record.
        assert knowledge["context"]["goals"]["revision"] == 1
        assert len(knowledge["assessments"]) == 1
        assert knowledge["assessments"][0]["metadata"]["counts"]["goal_gaps"] == 1
        assert [item["content"] for item in knowledge["task_candidates"]] == [TASK]
    finally:
        orchestrator.close()


async def test_a_suggestion_asks_for_a_decision_rather_than_sitting_in_the_inbox(tmp_path):
    runtime, _loop, orchestrator = await assessed(tmp_path)
    try:
        snapshot = cockpit(runtime).snapshot()

        waiting = [
            item
            for item in snapshot["needs_attention"]
            if item["kind"] == "task_candidate"
        ]
        assert [item["message"] for item in waiting] == [TASK]
        assert snapshot["overview"]["counts"]["task_candidates"] == 1
        # The context, the reading and the suggestion each have their own
        # section, so none of them should also be in the raw inbox.
        assert snapshot["knowledge"]["inbox"] == []
    finally:
        orchestrator.close()


async def test_running_a_suggestion_from_the_cockpit_reaches_the_orchestrator(tmp_path):
    runtime, loop, orchestrator = await assessed(tmp_path)
    try:
        candidate = loop.task_candidates(status=CANDIDATE_PENDING_REVIEW)[0]

        result = await cockpit(runtime).decide_task_candidate(
            candidate.knowledge_id, "approve"
        )

        assert result["status"] == CANDIDATE_ROUTED
        assert result["metadata"]["review_actor"] == "local-operator"
        assert orchestrator.projects.get(result["metadata"]["project_id"]) is not None
    finally:
        orchestrator.close()


async def test_ignoring_a_suggestion_starts_nothing(tmp_path):
    runtime, loop, orchestrator = await assessed(tmp_path)
    try:
        candidate = loop.task_candidates(status=CANDIDATE_PENDING_REVIEW)[0]

        result = await cockpit(runtime).decide_task_candidate(
            candidate.knowledge_id, "reject", note="いまは優先しない"
        )

        assert result["status"] == CANDIDATE_REJECTED
        assert orchestrator.projects.all() == []
    finally:
        orchestrator.close()


async def test_rewording_a_suggestion_keeps_it_waiting(tmp_path):
    runtime, loop, orchestrator = await assessed(tmp_path)
    try:
        candidate = loop.task_candidates(status=CANDIDATE_PENDING_REVIEW)[0]

        result = await cockpit(runtime).decide_task_candidate(
            candidate.knowledge_id, "amend", description="担当者に直接聞く"
        )

        assert result["status"] == CANDIDATE_PENDING_REVIEW
        assert result["content"] == "担当者に直接聞く"
        assert result["metadata"]["amended_from"] == TASK
        assert orchestrator.projects.all() == []
    finally:
        orchestrator.close()


async def test_a_cockpit_with_no_loop_reports_no_context_rather_than_failing(tmp_path):
    runtime = Runtime(tmp_path / "empty.db")
    try:
        knowledge = cockpit(runtime).snapshot()["knowledge"]

        assert knowledge["enabled"] is False
        assert knowledge["context"] == {}
        assert knowledge["task_candidates"] == []
        assert await cockpit(runtime).decide_task_candidate("x", "approve") is None
    finally:
        runtime.close()


# --- HTTP -------------------------------------------------------------------


async def test_the_three_decisions_are_reachable_over_http(tmp_path):
    runtime, loop, orchestrator = await assessed(tmp_path)
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        host="127.0.0.1",
        port=0,
        cockpit=cockpit(runtime),
    )
    await server.start()

    async def post(path, body):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = "\r\n".join(
            [
                f"POST {path} HTTP/1.1",
                "Host: 127.0.0.1",
                f"Authorization: Bearer {TOKEN}",
                "Content-Type: application/json",
                f"Content-Length: {len(payload)}",
            ]
        )
        writer.write((head + "\r\n\r\n").encode("latin-1") + payload)
        await writer.drain()
        response = await reader.read()
        writer.close()
        raw_head, _, raw = response.partition(b"\r\n\r\n")
        return int(raw_head.split(b"\r\n", 1)[0].split()[1]), json.loads(raw)

    candidate = loop.task_candidates(status=CANDIDATE_PENDING_REVIEW)[0]
    base = f"/cockpit/api/knowledge/task-candidates/{candidate.knowledge_id}"
    try:
        status, payload = await post(f"{base}/amend", {"description": "担当者に聞く"})
        assert status == 200
        assert payload["content"] == "担当者に聞く"

        status, payload = await post(f"{base}/approve", {})
        assert status == 200
        assert payload["status"] == CANDIDATE_ROUTED

        status, payload = await post(f"{base}/nonsense", {})
        assert status == 400

        status, _ = await post(
            "/cockpit/api/knowledge/task-candidates/nope/approve", {}
        )
        assert status == 404
    finally:
        await server.stop()
        orchestrator.close()


def test_the_page_offers_all_three_buttons():
    for action in ("task-candidate-approve", "task-candidate-reject", "task-candidate-amend"):
        assert action in APP_JS
    assert "Bootstrap Context" in APP_JS
