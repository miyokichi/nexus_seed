"""Closed loop: evidence -> proposal -> Project -> Agent -> Knowledge."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
import json

from nexus_seed.adapters import LocalFileAdapter
from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.cockpit import CockpitService
from nexus_seed.cockpit.assets import APP_JS
from nexus_seed.knowledge.autonomous_loop import (
    ARTIFACT_APPROVED,
    ARTIFACT_PENDING_REVIEW,
    ARTIFACT_REJECTED,
    KIND_AGENT_OBSERVATION,
    KIND_ARTIFACT,
    KIND_COMPLETION_REVIEW,
    KIND_PROJECT_PROPOSAL,
    KIND_QUESTION,
    KIND_RESOURCE_OBSERVATION,
    PROPOSAL_PENDING_REVIEW,
    PROPOSAL_ROUTED,
    KnowledgeLoop,
)
from nexus_seed.orchestrator import (
    A2AMessage,
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
)
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.processes.resources import bootstrap_observer, bootstrap_resources
from nexus_seed.resources.scope import ResourceScope
from nexus_seed.runtime.runtime import Runtime


def routing(goal="期限が近い契約を確認する"):
    return proposal_response(
        {
            "action": "CREATE_PROJECT",
            "proposed_goal": goal,
            "reason": "new evidence requires work",
            "confidence": 0.95,
        }
    )


def assessment(*, risk="low", confidence=0.9, read_only=True):
    return proposal_response(
        {
            "proposals": [
                {
                    "objective": "期限が近い契約を確認する",
                    "reason": "契約期限が7日以内",
                    # Replaced by EvidenceAwareBackend with the real id.
                    "evidence_ids": [],
                    "confidence": confidence,
                    "risk": risk,
                    "read_only": read_only,
                    "expected_artifacts": ["確認結果"],
                }
            ]
        }
    )


class EvidenceAwareBackend:
    """Return one proposal citing the evidence the loop actually supplied."""

    def __init__(self, result, *, only_once=True):
        self.result = result
        self.only_once = only_once
        self.calls = []

    async def execute(self, request):
        self.calls.append(request)
        if self.only_once and len(self.calls) > 1:
            return proposal_response({"proposals": []})
        output = dict(self.result.parsed_output)
        proposals = [dict(item) for item in output["proposals"]]
        evidence = request.context["new_evidence"]
        proposals[0]["evidence_ids"] = [evidence[0]["knowledge_id"]]
        return proposal_response({"proposals": proposals})


def completed(config, envelope):
    return [
        A2AMessage(
            type=A2AMessageType.PROJECT_COMPLETED,
            project_id=config.project_id,
            payload={
                "summary": "契約は7日後に期限を迎える",
                "artifacts": [
                    {
                        "name": "contract-check.md",
                        "media_type": "text/markdown",
                        "content": "# 確認結果\n更新判断が必要",
                    }
                ],
                "observations": [
                    {
                        "entity": "contract-A",
                        "entity_label": "契約A",
                        "attribute": "days_to_expiry",
                        "value": 7,
                        "confidence": 0.95,
                    }
                ],
                "questions": ["自動更新しますか？"],
            },
        )
    ]


def setup(tmp_path, *, evaluator, behaviour=completed):
    runtime = Runtime(tmp_path / "loop.db")
    orchestrator = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=behaviour),
        backend=FakeLLMBackend(default=routing()),
    )
    bootstrap_project_orchestration(runtime, orchestrator)
    loop = KnowledgeLoop(runtime, orchestrator, backend=evaluator)
    runtime.knowledge_loop = loop
    return runtime, orchestrator, loop


async def test_low_risk_evidence_closes_the_loop(tmp_path):
    evaluator = EvidenceAwareBackend(assessment())
    runtime, orchestrator, loop = setup(tmp_path, evaluator=evaluator)

    first = await loop.record_manual(
        "契約Aの期限は7日後です",
        source_event_key="manual-contract-a-v1",
    )
    await loop.reconcile()  # ingest the completion produced while routing

    assert first.accepted
    [project] = orchestrator.projects.all()
    assert project.status is ProjectStatus.WAITING_REVIEW
    assert project.context["knowledge_proposal_id"]
    assert project.context["evidence_ids"]

    [proposal] = loop.ledger.by_kind(KIND_PROJECT_PROPOSAL)
    assert proposal.status == PROPOSAL_ROUTED
    assert proposal.metadata["project_id"] == project.id
    artifact = loop.ledger.by_kind(KIND_ARTIFACT)[0]
    assert artifact.content.value.startswith("# 確認結果")
    assert artifact.status == ARTIFACT_PENDING_REVIEW
    assert loop.ledger.by_kind(KIND_COMPLETION_REVIEW)[0].status == ARTIFACT_PENDING_REVIEW
    assert loop.ledger.by_kind(KIND_QUESTION)[0].status == "OPEN"
    observation = loop.ledger.by_kind(KIND_AGENT_OBSERVATION)[0]
    assert observation.status == ARTIFACT_PENDING_REVIEW
    assert loop.world_view() == {}

    approved = await CockpitService(
        runtime, master_id="operator"
    ).decide_knowledge_artifact(artifact.knowledge_id, "approve")

    assert approved["status"] == ARTIFACT_APPROVED
    assert orchestrator.projects.get(project.id).status is ProjectStatus.COMPLETED
    assert loop.ledger.by_kind(KIND_AGENT_OBSERVATION)[0].status == "ACCEPTED"
    assert loop.world_view()["contract-A"]["days_to_expiry"] == 7

    # A UI snapshot reads the same source records; it is not a second model.
    knowledge = CockpitService(
        runtime, master_id="operator"
    ).snapshot()["knowledge"]
    assert knowledge["enabled"] is True
    assert knowledge["world"]["facts"][0]["entity"] == "contract-A"
    assert knowledge["artifacts"][0]["metadata"]["project_id"] == project.id
    runtime.close()


async def test_medium_risk_waits_for_human_then_routes_once(tmp_path):
    evaluator = EvidenceAwareBackend(
        assessment(risk="medium", confidence=0.9, read_only=False)
    )
    runtime, orchestrator, loop = setup(
        tmp_path, evaluator=evaluator, behaviour=lambda config, envelope: []
    )

    await loop.record_manual("顧客への通知が必要かもしれない", source_event_key="manual-2")

    [proposal] = loop.proposals()
    assert proposal.status == PROPOSAL_PENDING_REVIEW
    assert orchestrator.projects.all() == []

    approved = await loop.decide_proposal(
        proposal.knowledge_id, "approve", actor="operator"
    )

    assert approved.status == PROPOSAL_ROUTED
    assert len(orchestrator.projects.all()) == 1
    # Reconciliation and a repeated approval converge on the same Project.
    await loop.reconcile()
    await loop.decide_proposal(proposal.knowledge_id, "approve", actor="operator")
    assert len(orchestrator.projects.all()) == 1
    runtime.close()


async def test_agent_question_returns_answer_to_the_same_project(tmp_path):
    calls = {"count": 0}

    def asks_then_completes(config, envelope):
        calls["count"] += 1
        if calls["count"] == 1:
            return [
                A2AMessage(
                    type=A2AMessageType.NEED_HUMAN_INPUT,
                    project_id=config.project_id,
                    payload={
                        "reason": "更新方針が不明",
                        "questions": ["自動更新しますか？"],
                    },
                )
            ]
        return [
            A2AMessage(
                type=A2AMessageType.PROJECT_COMPLETED,
                project_id=config.project_id,
                payload={"summary": "更新しない方針を記録した"},
            )
        ]

    runtime, orchestrator, loop = setup(
        tmp_path,
        evaluator=EvidenceAwareBackend(assessment()),
        behaviour=asks_then_completes,
    )
    await loop.record_manual("契約Aの期限は7日後です", source_event_key="question-input")
    await loop.reconcile()
    [project] = orchestrator.projects.all()
    assert project.status is ProjectStatus.WAITING_HUMAN
    [question] = loop.questions()

    answered = await loop.answer_question(
        question.knowledge_id, "今回は自動更新しない", actor="operator"
    )
    await loop.reconcile()
    [completion_review] = loop.completion_reviews(status=ARTIFACT_PENDING_REVIEW)
    await loop.decide_completion_review(
        completion_review.knowledge_id, "approve", actor="operator"
    )

    assert answered.status == "ANSWERED"
    current = orchestrator.projects.get(project.id)
    assert current.status is ProjectStatus.COMPLETED
    assert [task["description"] for task in current.tasks] == [
        "Human answer: 今回は自動更新しない"
    ]
    assert len(orchestrator.projects.all()) == 1
    runtime.close()


async def test_rejected_artifact_returns_to_same_project_and_agent_once(tmp_path):
    calls = {"count": 0}

    def completes_then_waits(config, envelope):
        calls["count"] += 1
        if calls["count"] == 1:
            return completed(config, envelope)
        return []

    runtime, orchestrator, loop = setup(
        tmp_path,
        evaluator=EvidenceAwareBackend(assessment()),
        behaviour=completes_then_waits,
    )
    await loop.record_manual("契約Aの期限は7日後です", source_event_key="reject-input")
    await loop.reconcile()
    [project] = orchestrator.projects.all()
    agent_id = project.assigned_agent_id
    [artifact] = loop.artifacts()

    rejected = await loop.decide_artifact(
        artifact.knowledge_id,
        "reject",
        actor="operator",
        note="更新判断の根拠を追記してください",
    )
    # A repeated click sees the settled HEAD and cannot add a second Task.
    again = await loop.decide_artifact(
        artifact.knowledge_id,
        "reject",
        actor="operator",
        note="更新判断の根拠を追記してください",
    )

    assert rejected.status == ARTIFACT_REJECTED
    assert again.id == rejected.id
    current = orchestrator.projects.get(project.id)
    assert current.status is ProjectStatus.ACTIVE
    assert current.assigned_agent_id == agent_id
    assert [task["description"] for task in current.tasks] == [
        "成果物を修正してください: 更新判断の根拠を追記してください"
    ]
    assert len(orchestrator.projects.all()) == 1
    assert loop.completion_reviews()[0].status == ARTIFACT_REJECTED
    runtime.close()


async def test_completion_review_survives_restart_and_approval_is_idempotent(tmp_path):
    runtime, orchestrator, loop = setup(
        tmp_path,
        evaluator=EvidenceAwareBackend(assessment()),
        behaviour=completed,
    )
    await loop.record_manual("契約Aの期限は7日後です", source_event_key="review-restart")
    await loop.reconcile()
    [project] = orchestrator.projects.all()
    [artifact] = loop.artifacts()
    project_id, artifact_id = project.id, artifact.knowledge_id
    runtime.close()

    second_runtime = Runtime(tmp_path / "loop.db")
    second_orchestrator = ProjectOrchestrator(
        second_runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=lambda config, envelope: []),
        backend=FakeLLMBackend(default=routing()),
    )
    bootstrap_project_orchestration(second_runtime, second_orchestrator)
    second_loop = KnowledgeLoop(second_runtime, second_orchestrator, backend=None)
    second_runtime.knowledge_loop = second_loop

    assert second_orchestrator.projects.get(project_id).status is ProjectStatus.WAITING_REVIEW
    assert second_loop.ledger.head(artifact_id).status == ARTIFACT_PENDING_REVIEW
    approved = await second_loop.decide_artifact(
        artifact_id, "approve", actor="operator"
    )
    history_size = len(second_loop.ledger.history(artifact_id))
    again = await second_loop.decide_artifact(
        artifact_id, "approve", actor="operator"
    )

    assert approved.status == ARTIFACT_APPROVED
    assert again.id == approved.id
    assert len(second_loop.ledger.history(artifact_id)) == history_size
    assert second_orchestrator.projects.get(project_id).status is ProjectStatus.COMPLETED
    second_runtime.close()


async def test_duplicate_completion_message_creates_one_review(tmp_path):
    fixed = A2AMessage(
        id="completion-message-1",
        type=A2AMessageType.PROJECT_COMPLETED,
        payload={
            "summary": "完了",
            "artifacts": [{"name": "result.md", "content": "結果"}],
        },
    )

    runtime, orchestrator, loop = setup(
        tmp_path,
        evaluator=EvidenceAwareBackend(assessment()),
        behaviour=lambda config, envelope: [fixed, fixed],
    )
    await loop.record_manual("確認対象があります", source_event_key="duplicate-completion")
    await loop.reconcile()

    [project] = orchestrator.projects.all()
    assert project.status is ProjectStatus.WAITING_REVIEW
    assert len(project.current_blockers) == 1
    assert len(loop.artifacts()) == 1
    assert len(loop.completion_reviews()) == 1
    inbound = [
        message for direction, message in orchestrator.message_store.all()
        if direction == "inbound" and message.id == fixed.id
    ]
    assert len(inbound) == 1
    runtime.close()


async def test_cockpit_artifact_review_endpoint_and_controls(tmp_path):
    runtime, orchestrator, loop = setup(
        tmp_path,
        evaluator=EvidenceAwareBackend(assessment()),
        behaviour=completed,
    )
    await loop.record_manual("契約Aの期限は7日後です", source_event_key="cockpit-review")
    await loop.reconcile()
    [artifact] = loop.artifacts()
    cockpit = CockpitService(runtime, master_id="operator")
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token="review-token"), cockpit=cockpit
    )

    unauthorized = await server._knowledge_action_response(
        f"/cockpit/api/knowledge/artifacts/{artifact.knowledge_id}/approve",
        {},
        b"{}",
    )
    approved = await server._knowledge_action_response(
        f"/cockpit/api/knowledge/artifacts/{artifact.knowledge_id}/approve",
        {"authorization": "Bearer review-token"},
        json.dumps({"note": "確認済み"}).encode(),
    )

    assert unauthorized.status_code == 401
    assert approved.status_code == 200
    assert approved.body["status"] == ARTIFACT_APPROVED
    assert orchestrator.projects.all()[0].status is ProjectStatus.COMPLETED
    assert "knowledge-artifact-approve" in APP_JS
    assert "knowledge-artifact-reject" in APP_JS
    assert "knowledge-completion-approve" in APP_JS
    assert "差し戻し理由を入力してください" in APP_JS
    runtime.close()


async def test_restart_does_not_duplicate_knowledge_or_projects(tmp_path):
    evaluator = EvidenceAwareBackend(assessment())
    runtime, orchestrator, loop = setup(
        tmp_path, evaluator=evaluator, behaviour=lambda config, envelope: []
    )
    await loop.record_manual("契約Aの期限は7日後です", source_event_key="stable-input")
    before_ids = {item.knowledge_id for item in loop.ledger.all_heads()}
    [project] = orchestrator.projects.all()
    project_id = project.id
    runtime.close()

    second_runtime = Runtime(tmp_path / "loop.db")
    second_orchestrator = ProjectOrchestrator(
        second_runtime.db,
        agent_runtime=InProcessAgentRuntime(),
        backend=FakeLLMBackend(default=routing()),
    )
    bootstrap_project_orchestration(second_runtime, second_orchestrator)
    second_loop = KnowledgeLoop(
        second_runtime,
        second_orchestrator,
        backend=FakeLLMBackend(default=proposal_response({"proposals": []})),
    )
    second_runtime.knowledge_loop = second_loop

    await second_loop.reconcile()
    duplicate = await second_loop.record_manual(
        "契約Aの期限は7日後です", source_event_key="stable-input"
    )

    assert duplicate.duplicate
    assert {item.knowledge_id for item in second_loop.ledger.all_heads()} == before_ids
    assert [item.id for item in second_orchestrator.projects.all()] == [project_id]
    second_runtime.close()


async def test_authorized_folder_enters_knowledge_and_has_one_observer(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "situation.txt").write_text(
        "契約Aの更新期限が近い", encoding="utf-8"
    )
    runtime = Runtime(tmp_path / "files.db")
    bootstrap_resources(runtime, scope=ResourceScope.for_root(inbox))
    bootstrap_observer(runtime)
    runtime.register_adapter(
        LocalFileAdapter(inbox, adapter_id="knowledge_local_file")
    )
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator, backend=None)

    await loop.start_file_observer(poll_interval=3600)
    await loop.reconcile()
    await loop.start_file_observer(poll_interval=3600)

    [knowledge] = loop.ledger.by_kind(KIND_RESOURCE_OBSERVATION)
    assert knowledge.content.value == "契約Aの更新期限が近い"
    watchers = [
        item
        for item in runtime.process_store.all_instances()
        if item.definition_name == "watch_files"
    ]
    assert len(watchers) == 1
    assert watchers[0].status.value == "SUSPENDED"
    runtime.close()
