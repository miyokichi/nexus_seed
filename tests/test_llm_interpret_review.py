"""AT3 / AT7: medium confidence -> REVIEW (suspend); low confidence -> REJECT."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.intelligence.proposal import ProposalDecision
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime

MESSAGE = "D1のCD targetを48nmから45nmへ変更しました。"


def proposal_dict(confidence, *, old=48, new=45):
    return {
        "subject": "D1_CD",
        "predicate": "target_changed",
        "confidence": confidence,
        "rationale": "message reports an nm target change",
        "proposed_state_deltas": [
            {"entity": "D1_CD", "attribute": "target", "old_value": old, "new_value": new, "unit": "nm", "confidence": confidence}
        ],
    }


async def test_medium_confidence_goes_to_review(tmp_path):
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.70))])
    runtime = Runtime(tmp_path / "review.db")
    bootstrap_semantic(runtime)
    bootstrap_llm_interpreter(runtime, backend)

    await runtime.submit_event(Event("human_message", "user", {"text": MESSAGE}))

    instance = [
        i
        for i in runtime.process_store.all_instances()
        if i.definition_name == "interpret_event_llm"
    ][0]
    assert instance.status is ProcessStatus.SUSPENDED

    # A proposal in REVIEW, a continuation waiting for the human, no world change.
    proposal = runtime.get_proposals()[0]
    assert proposal.decision is ProposalDecision.REVIEW
    continuation = runtime.continuation_store.for_instance(instance.id)
    assert continuation.waiting_for == {
        "event_type": "interpretation_reviewed",
        "proposal_id": str(proposal.id),
    }
    assert runtime.state_store.get("D1_CD", "target") is None
    assert runtime.observation_store.all() == []
    assert runtime.state_delta_store.all() == []
    runtime.close()


async def test_low_confidence_is_rejected(tmp_path):
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.40))])
    runtime = Runtime(tmp_path / "reject.db")
    bootstrap_semantic(runtime)
    bootstrap_llm_interpreter(runtime, backend)

    produced = await runtime.submit_event(Event("human_message", "user", {"text": MESSAGE}))

    instance = [
        i
        for i in runtime.process_store.all_instances()
        if i.definition_name == "interpret_event_llm"
    ][0]
    assert instance.status is ProcessStatus.COMPLETED
    assert runtime.get_proposals()[0].decision is ProposalDecision.REJECT
    assert runtime.state_store.get("D1_CD", "target") is None
    assert runtime.observation_store.all() == []
    assert "interpretation_rejected" in [e.type for e in produced]
    runtime.close()
