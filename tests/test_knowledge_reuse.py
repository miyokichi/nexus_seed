"""Cross-project reuse of explicitly shared human answers.

These tests use the real Project Orchestrator, A2A journal, Knowledge Ledger
and SQLite persistence; only the Agent's decisions are scripted.
"""

from __future__ import annotations

import pytest

from nexus_seed.knowledge import Annotation, KnowledgeLedger
from nexus_seed.knowledge.autonomous_loop import KnowledgeLoop
from nexus_seed.knowledge.reuse import (
    KnowledgeReuse,
    REUSABLE_ANSWER_ANNOTATION,
    normalize_reuse_spec,
)
from nexus_seed.orchestrator import (
    A2AMessage,
    A2AMessageType,
    InProcessAgentRuntime,
    ProjectOrchestrator,
    ProjectStatus,
)
from nexus_seed.processes.project_orchestration import bootstrap_project_orchestration
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.storage import Database, KnowledgeStore


SCOPE = {
    "subject": "resistance_model_R1",
    "property": "resistivity",
    "conditions": {"cross_section": "rectangle", "temperature": "constant"},
}


def test_reuse_requires_exact_scope_and_human_annotation(tmp_path):
    ledger = KnowledgeLedger(KnowledgeStore(Database(tmp_path / "knowledge.db")))
    question = ledger.record(
        "Which resistivity should R1 use?",
        source_type="project_agent_question",
        kind="question",
        status="ANSWERED",
        metadata={"answer": "rho = 0.02 ohm_um"},
    )
    lookup = KnowledgeReuse(ledger)
    request = {"knowledge_requirements": [SCOPE]}

    # Human answers are NOT automatically shareable.
    assert lookup.for_project_context(request) == []
    ledger.annotate(
        question.knowledge_id,
        Annotation(kind=REUSABLE_ANSWER_ANNOTATION, value=SCOPE, created_by="human"),
    )
    [result] = lookup.for_project_context(request)
    assert result["knowledge_id"] == question.knowledge_id
    assert result["answer"] == "rho = 0.02 ohm_um"
    assert result["verification"] == "unverified"

    assert lookup.for_project_context({"knowledge_requirements": [{
        **SCOPE,
        "conditions": {"cross_section": "trapezoid", "temperature": "constant"},
    }]}) == []
    assert lookup.for_project_context({"knowledge_requirements": [{
        "subject": SCOPE["subject"], "property": SCOPE["property"],
    }]}) == []
    assert lookup.for_project_context({"knowledge_requirements": [SCOPE, SCOPE]}) == [result]

    # Two conflicting human answers must not be silently resolved.
    other = ledger.record(
        "A competing answer", source_type="human", kind="question", status="ANSWERED",
        metadata={"answer": "rho = 0.03 ohm_um"},
    )
    ledger.annotate(
        other.knowledge_id,
        Annotation(kind=REUSABLE_ANSWER_ANNOTATION, value=SCOPE, created_by="human"),
    )
    assert lookup.for_project_context(request) == []


@pytest.mark.parametrize("invalid", [
    {"subject": "R1", "property": "", "conditions": {}},
    {"subject": "R1", "property": "resistivity", "conditions": ["wrong"]},
    {"subject": "R1", "property": "resistivity", "conditions": {"x": None}},
])
def test_reuse_scope_rejects_invalid_conditions(invalid):
    with pytest.raises(ValueError):
        normalize_reuse_spec(invalid)


async def test_human_answer_reaches_second_project_but_not_incompatible_one(tmp_path):
    db_path = tmp_path / "loop.db"
    contexts = []

    def scripted_agent(config, envelope):
        contexts.append((config.project_id, dict(config.project_context)))
        if config.project_context.get("reusable_knowledge"):
            return [A2AMessage(
                type=A2AMessageType.PROJECT_COMPLETED,
                project_id=config.project_id,
                payload={"summary": "Used supplied R1 assumption"},
            )]
        return [A2AMessage(
            type=A2AMessageType.NEED_HUMAN_INPUT,
            project_id=config.project_id,
            payload={"reason": "R1 resistivity unknown", "questions": ["Which rho for R1?"]},
        )]

    runtime = Runtime(db_path)
    orchestrator = ProjectOrchestrator(
        runtime.db,
        agent_runtime=InProcessAgentRuntime(behaviour=scripted_agent),
    )
    bootstrap_project_orchestration(runtime, orchestrator)
    loop = KnowledgeLoop(runtime, orchestrator, backend=None)
    runtime.knowledge_loop = loop

    first = await orchestrator.start_project(
        "Calculate resistance with R1",
        context={"knowledge_requirements": [SCOPE]},
    )
    await loop.reconcile()
    assert orchestrator.projects.get(first.id).status is ProjectStatus.WAITING_HUMAN
    [question] = loop.questions()
    answer = await loop.answer_question(
        question.knowledge_id, "rho = 0.02 ohm_um", actor="operator", reuse=SCOPE,
    )
    assert answer.status == "ANSWERED"
    assert any(
        a.kind == REUSABLE_ANSWER_ANNOTATION for a in answer.annotations
    )
    await loop.reconcile()

    second = await orchestrator.start_project(
        "Calculate another rectangular R1 wire",
        context={"knowledge_requirements": [SCOPE]},
    )
    assert second.id != first.id
    seen_second = [ctx for id_, ctx in contexts if id_ == second.id]
    assert len(seen_second) == 1
    [reused] = seen_second[0]["reusable_knowledge"]
    assert reused["knowledge_id"] == question.knowledge_id
    assert reused["conditions"] == SCOPE["conditions"]

    incompatible = {
        **SCOPE,
        "conditions": {"cross_section": "trapezoid", "temperature": "constant"},
    }
    third = await orchestrator.start_project(
        "Calculate trapezoid R1 resistance",
        context={"knowledge_requirements": [incompatible]},
    )
    seen_third = [ctx for id_, ctx in contexts if id_ == third.id]
    assert "reusable_knowledge" not in seen_third[0]
    assert orchestrator.projects.get(third.id).status is ProjectStatus.WAITING_HUMAN
    runtime.close()

    # The source Ledger survives a restart and the read view is regenerated.
    runtime2 = Runtime(db_path)
    agent2 = InProcessAgentRuntime(behaviour=scripted_agent)
    orchestrator2 = ProjectOrchestrator(runtime2.db, agent_runtime=agent2)
    bootstrap_project_orchestration(runtime2, orchestrator2)
    KnowledgeLoop(runtime2, orchestrator2, backend=None)
    fourth = await orchestrator2.start_project(
        "Re-use R1 assumption after restart",
        context={"knowledge_requirements": [SCOPE]},
    )
    seen_fourth = [ctx for id_, ctx in contexts if id_ == fourth.id]
    assert seen_fourth[0]["reusable_knowledge"][0]["knowledge_id"] == question.knowledge_id
    runtime2.close()
