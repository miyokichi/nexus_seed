"""Bootstrap context -> assessment -> task candidate -> a person -> orchestrator.

The v0.1 acceptance path, and the boundaries around it: goals stay prose,
nothing runs without a person, and a person's edit is kept.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.knowledge.bootstrap_context import ContextDocuments
from nexus_seed.knowledge.context_assessment import (
    ASSIGNEE_AGENT,
    CANDIDATE_PENDING_REVIEW,
    CANDIDATE_REJECTED,
    CANDIDATE_ROUTED,
    KIND_CONTEXT_ASSESSMENT,
    ContextAssessor,
    SituationAssessment,
    candidate_id,
)
from nexus_seed.knowledge.ledger import KnowledgeLedger
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.orchestrator.models import Project, ProjectStatus
from nexus_seed.storage import Database, KnowledgeStore

GOAL_GAP = "BLOCKED Projectが存在する"
TASK = "Project Aのblockerを調査する"


def assessment_payload(**overrides):
    payload = {
        "terminology_issues": [],
        "contradictions": [],
        "goal_gaps": [
            {
                "description": GOAL_GAP,
                "evidence": ["goals.md: BLOCKED Projectを放置しない", "Project A"],
                "confidence": 0.9,
            }
        ],
        "unknowns": [],
        "task_candidates": [
            {
                "description": TASK,
                "reason": "goals.mdはBLOCKEDを放置しないと述べているが、Project Aは止まっている",
                "evidence": ["situation.md: Project Aはデータ不足でBLOCKED"],
                "confidence": 0.8,
                "suggested_assignee_type": "AGENT",
            }
        ],
    }
    payload.update(overrides)
    return payload


class Backend(FakeLLMBackend):
    """Answers a context assessment; anything else gets an empty answer."""

    def __init__(self, payload=None):
        super().__init__()
        self.payload = payload if payload is not None else assessment_payload()

    async def execute(self, request):
        self.calls.append(request)
        if request.metadata.get("kind") == "context_assessment":
            return proposal_response(self.payload)
        return proposal_response({})


def build(tmp_path, backend=None, *, routing=None):
    db = Database(str(tmp_path / "k.db"))
    ledger = KnowledgeLedger(KnowledgeStore(db))
    orchestrator = ProjectOrchestrator(
        db, agent_runtime=InProcessAgentRuntime(), backend=routing
    )
    documents = ContextDocuments(ledger, tmp_path / "context")
    documents.ensure()
    assessor = ContextAssessor(ledger, documents, orchestrator, backend=backend)
    return assessor, documents, orchestrator, db


def write(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / "context" / f"{name}.md").write_text(text, encoding="utf-8")


def blocked_project(orchestrator, goal="Project A"):
    project = Project(goal=goal, status=ProjectStatus.BLOCKED)
    orchestrator.project_store.save(project)
    return project


# --- the v0.1 acceptance path ----------------------------------------------


async def test_the_acceptance_path_runs_end_to_end(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(
            tmp_path,
            goals="BLOCKED Projectを放置しない",
            situation="Project Aはデータ不足でBLOCKED",
        )
        documents.sync()
        blocked_project(orchestrator)

        recorded = await assessor.assess()

        assert recorded is not None
        assert recorded.kind == KIND_CONTEXT_ASSESSMENT
        assert [item["description"] for item in recorded.content.value["goal_gaps"]] == [
            GOAL_GAP
        ]

        pending = assessor.pending()
        assert [item.content.value for item in pending] == [TASK]
        assert pending[0].metadata["suggested_assignee_type"] == ASSIGNEE_AGENT

        # Nothing has been started: a candidate is a suggestion until a person
        # says otherwise.
        assert len(orchestrator.projects.all()) == 1

        settled = await assessor.decide(pending[0].knowledge_id, "approve")

        assert settled.status == CANDIDATE_ROUTED
        assert settled.metadata["project_id"]
        assert len(orchestrator.projects.all()) == 2
        routed = orchestrator.projects.get(settled.metadata["project_id"])
        assert routed.context["task_candidate_id"] == pending[0].knowledge_id
    finally:
        orchestrator.close()


async def test_an_approved_task_can_join_a_project_that_already_exists(tmp_path):
    # The router owns this choice, not the assessor: submit() is the same door
    # a human message goes through, so a candidate about running work becomes
    # another Task rather than a second Project about the same thing.
    from nexus_seed.backends import proposal_response

    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        existing = blocked_project(orchestrator)
        await assessor.assess()
        candidate = assessor.pending()[0]

        orchestrator.router.backend = FakeLLMBackend(
            default=proposal_response(
                {
                    "action": "ADD_TASK_TO_PROJECT",
                    "target_project_id": existing.id,
                    "proposed_task": TASK,
                    "reason": "同じProjectの話",
                    "confidence": 0.9,
                }
            )
        )
        settled = await assessor.decide(candidate.knowledge_id, "approve")

        assert settled.status == CANDIDATE_ROUTED
        assert settled.metadata["routing_action"] == "ADD_TASK_TO_PROJECT"
        assert len(orchestrator.projects.all()) == 1
        assert [task["description"] for task in
                orchestrator.projects.get(existing.id).tasks] == [TASK]
    finally:
        orchestrator.close()


async def test_the_prompt_is_shown_the_context_the_world_and_the_projects(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        blocked_project(orchestrator)

        await assessor.assess()

        context = backend.calls[-1].context
        assert set(context["context_documents"]) == {"terms", "goals", "situation"}
        assert context["context_documents"]["goals"]["text"] == "BLOCKEDを放置しない"
        assert "world_view" in context
        assert len(context["projects"]["blocked"]) == 1
        assert context["projects"]["active"] == []
    finally:
        orchestrator.close()


# --- what must not happen ---------------------------------------------------


async def test_without_a_backend_nothing_is_assessed_and_nothing_is_invented(tmp_path):
    assessor, documents, orchestrator, db = build(tmp_path, None)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない")
        documents.sync()

        assert await assessor.assess() is None
        assert assessor.assessments() == []
        assert assessor.pending() == []
    finally:
        orchestrator.close()


async def test_an_unchanged_situation_is_not_read_twice(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        await assessor.assess()
        calls = len(backend.calls)

        assert await assessor.assess() is None
        assert len(backend.calls) == calls
    finally:
        orchestrator.close()


async def test_a_project_changing_status_is_a_new_situation(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="順調")
        documents.sync()
        await assessor.assess()
        calls = len(backend.calls)

        project = blocked_project(orchestrator)
        assert await assessor.assess() is not None
        assert len(backend.calls) > calls
        assert project.status is ProjectStatus.BLOCKED
    finally:
        orchestrator.close()


async def test_a_rejected_candidate_is_kept_and_never_routed(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        await assessor.assess()
        candidate = assessor.pending()[0]

        settled = await assessor.decide(
            candidate.knowledge_id, "reject", note="いまはやらない"
        )

        assert settled.status == CANDIDATE_REJECTED
        assert settled.metadata["review_note"] == "いまはやらない"
        assert settled.content.value == TASK
        assert orchestrator.projects.all() == []
    finally:
        orchestrator.close()


async def test_the_same_suggestion_does_not_queue_twice(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        await assessor.assess()
        await assessor.decide(assessor.pending()[0].knowledge_id, "reject")

        # A later reading of a changed situation suggests the same thing again.
        write(tmp_path, situation="Aはまだ止まっている")
        documents.sync()
        await assessor.assess()

        assert assessor.pending() == []
        assert len(assessor.candidates()) == 1
        assert assessor.candidates()[0].status == CANDIDATE_REJECTED
    finally:
        orchestrator.close()


# --- a person's edit is evidence --------------------------------------------


async def test_amending_replaces_the_wording_and_keeps_the_original(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        await assessor.assess()
        candidate = assessor.pending()[0]

        amended = await assessor.decide(
            candidate.knowledge_id,
            "amend",
            description="Project Aの担当者に直接確認する",
            note="調査より先に人に聞くほうが早い",
        )

        assert amended.content.value == "Project Aの担当者に直接確認する"
        assert amended.status == CANDIDATE_PENDING_REVIEW
        assert amended.metadata["amended_from"] == TASK
        assert amended.metadata["amendment_note"] == "調査より先に人に聞くほうが早い"
        history = assessor.ledger.history(candidate.knowledge_id)
        assert history[0].content.value == TASK
    finally:
        orchestrator.close()


async def test_an_amended_task_is_what_gets_routed(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        await assessor.assess()
        candidate = assessor.pending()[0]
        await assessor.decide(
            candidate.knowledge_id, "amend", description="担当者に直接確認する"
        )

        settled = await assessor.decide(candidate.knowledge_id, "approve")

        assert settled.status == CANDIDATE_ROUTED
        project = orchestrator.projects.get(settled.metadata["project_id"])
        assert "担当者に直接確認する" in project.goal
    finally:
        orchestrator.close()


async def test_amending_something_already_running_is_refused(tmp_path):
    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        await assessor.assess()
        candidate = assessor.pending()[0]
        await assessor.decide(candidate.knowledge_id, "approve")

        unchanged = await assessor.decide(
            candidate.knowledge_id, "amend", description="やっぱり別のこと"
        )

        assert unchanged.status == CANDIDATE_ROUTED
        assert unchanged.content.value == TASK
    finally:
        orchestrator.close()


async def test_an_amendment_needs_words(tmp_path):
    import pytest

    backend = Backend()
    assessor, documents, orchestrator, db = build(tmp_path, backend)
    try:
        write(tmp_path, goals="BLOCKEDを放置しない", situation="Aが止まっている")
        documents.sync()
        await assessor.assess()
        candidate = assessor.pending()[0]

        with pytest.raises(ValueError):
            await assessor.decide(candidate.knowledge_id, "amend", description="  ")
    finally:
        orchestrator.close()


# --- parsing ----------------------------------------------------------------


def test_a_finding_with_no_words_is_dropped_rather_than_shown():
    assessment = SituationAssessment.from_payload(
        {
            "goal_gaps": [{"description": ""}, {"description": "本物"}, "not a dict"],
            "task_candidates": [{"description": "", "reason": "x"}],
        }
    )

    assert [item.description for item in assessment.goal_gaps] == ["本物"]
    assert assessment.task_candidates == []


def test_an_unknown_assignee_becomes_UNKNOWN_rather_than_being_believed():
    assessment = SituationAssessment.from_payload(
        {
            "task_candidates": [
                {"description": "x", "reason": "y", "suggested_assignee_type": "ROBOT"}
            ]
        }
    )

    assert assessment.task_candidates[0].suggested_assignee_type == "UNKNOWN"


def test_an_empty_answer_is_a_valid_answer():
    assert SituationAssessment.from_payload({}).empty
    assert SituationAssessment.from_payload(None).empty


def test_spacing_and_case_do_not_make_it_a_different_candidate():
    # Two readings that say the same thing must land on one object, or a
    # person rejects the suggestion and is shown it again next tick.
    assert candidate_id("Project Aのblockerを調査する") == candidate_id(
        "  Project  Aのblockerを調査する\n"
    )
    assert candidate_id("Investigate the blocker") == candidate_id(
        "investigate the blocker"
    )
    assert candidate_id("Aを調査する") != candidate_id("Bを調査する")
