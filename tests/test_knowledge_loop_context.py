"""The bootstrap context inside the running loop.

Two things matter here and nothing else does: a loop with no context root
behaves exactly as it did before this existed, and a loop with one carries the
whole path — sync, assess, wait for a person, submit.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge.autonomous_loop import KnowledgeLoop
from nexus_seed.knowledge.context_assessment import (
    CANDIDATE_PENDING_REVIEW,
    CANDIDATE_ROUTED,
)
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.orchestrator.models import Project, ProjectStatus
from nexus_seed.runtime.runtime import Runtime

TASK = "Project Aのblockerを調査する"


class Backend(FakeLLMBackend):
    """Answers the context assessment; every other call gets nothing."""

    async def execute(self, request):
        self.calls.append(request)
        if request.metadata.get("kind") == "context_assessment":
            return proposal_response(
                {
                    "terminology_issues": [],
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


def build(tmp_path, *, context_root=None, backend=None):
    runtime = Runtime(tmp_path / "loop.db")
    orchestrator = ProjectOrchestrator(runtime.db, agent_runtime=InProcessAgentRuntime())
    loop = KnowledgeLoop(
        runtime, orchestrator, backend=backend, context_root=context_root
    )
    runtime.knowledge_loop = loop
    return runtime, loop, orchestrator


async def test_a_loop_without_a_context_root_does_nothing_new(tmp_path):
    runtime, loop, _orch = build(tmp_path, backend=Backend())
    try:
        result = await loop.reconcile()

        assert loop.context_documents is None
        assert loop.context_assessor is None
        assert result.context_documents == 0
        assert result.context_assessments == 0
        assert result.task_candidates_routed == 0
        assert loop.context() == {}
        assert loop.assessments() == []
        assert loop.task_candidates() == []
        assert await loop.decide_task_candidate("anything", "approve") is None
        assert not (tmp_path / "context").exists()
    finally:
        runtime.close()


async def test_one_pass_reads_the_context_and_files_a_candidate(tmp_path):
    backend = Backend()
    root = tmp_path / "context"
    runtime, loop, orchestrator = build(tmp_path, context_root=root, backend=backend)
    try:
        loop.context_documents.ensure()
        (root / "goals.md").write_text("BLOCKED Projectを放置しない", encoding="utf-8")
        (root / "situation.md").write_text(
            "Project Aはデータ不足でBLOCKED", encoding="utf-8"
        )
        orchestrator.project_store.save(
            Project(goal="Project A", status=ProjectStatus.BLOCKED)
        )

        result = await loop.reconcile()

        assert result.context_documents == 3
        assert result.context_assessments == 1
        assert result.task_candidates_routed == 0
        pending = loop.task_candidates(status=CANDIDATE_PENDING_REVIEW)
        assert [item.content.value for item in pending] == [TASK]
        # Still one project: nothing runs without a person.
        assert len(orchestrator.projects.all()) == 1
    finally:
        runtime.close()


async def test_a_quiet_tick_costs_nothing(tmp_path):
    backend = Backend()
    root = tmp_path / "context"
    runtime, loop, _orch = build(tmp_path, context_root=root, backend=backend)
    try:
        loop.context_documents.ensure()
        (root / "goals.md").write_text("BLOCKEDを放置しない", encoding="utf-8")
        await loop.reconcile()
        assessments = len(
            [call for call in backend.calls
             if call.metadata.get("kind") == "context_assessment"]
        )

        result = await loop.reconcile()

        assert result.context_documents == 0
        assert result.context_assessments == 0
        assert len(
            [call for call in backend.calls
             if call.metadata.get("kind") == "context_assessment"]
        ) == assessments
    finally:
        runtime.close()


async def test_approving_through_the_loop_reaches_the_orchestrator(tmp_path):
    backend = Backend()
    root = tmp_path / "context"
    runtime, loop, orchestrator = build(tmp_path, context_root=root, backend=backend)
    try:
        loop.context_documents.ensure()
        (root / "goals.md").write_text("BLOCKEDを放置しない", encoding="utf-8")
        (root / "situation.md").write_text("Aが止まっている", encoding="utf-8")
        await loop.reconcile()
        candidate = loop.task_candidates(status=CANDIDATE_PENDING_REVIEW)[0]

        settled = await loop.decide_task_candidate(
            candidate.knowledge_id, "approve", actor="operator"
        )

        assert settled.status == CANDIDATE_ROUTED
        project = orchestrator.projects.get(settled.metadata["project_id"])
        assert project is not None
        assert project.context["task_candidate_id"] == candidate.knowledge_id
    finally:
        runtime.close()


async def test_a_broken_context_file_does_not_stop_the_loop(tmp_path):
    backend = Backend()
    root = tmp_path / "context"
    runtime, loop, _orch = build(tmp_path, context_root=root, backend=backend)
    try:
        root.mkdir(parents=True)
        (root / "goals.md").write_bytes(b"\xff\xfe not text at all")

        result = await loop.reconcile()

        assert result.errors == []
        assert result.context_documents == 0
    finally:
        runtime.close()
