"""AT8 + AT12: state-conflict cannot auto-accept; LLM cannot bypass validation."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.intelligence.proposal import ProposalDecision
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


def proposal_dict(confidence, *, old, new=45):
    return {
        "subject": "D1_CD",
        "predicate": "target_changed",
        "confidence": confidence,
        "rationale": "nm change",
        "proposed_state_deltas": [
            {"entity": "D1_CD", "attribute": "target", "old_value": old, "new_value": new, "unit": "nm", "confidence": confidence}
        ],
    }


async def test_state_conflict_forces_review_despite_high_confidence(tmp_path):
    # Current world says target == 48; the proposal claims old_value == 50.
    runtime = Runtime(tmp_path / "conflict.db")
    bootstrap_semantic(runtime)
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.99, old=50))])
    bootstrap_llm_interpreter(runtime, backend)
    runtime.state_store.set("D1_CD", "target", 48)

    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "interpret_event_llm"
    ][0]
    # Confidence 0.99 but a state conflict -> REVIEW, not ACCEPT.
    assert instance.status is ProcessStatus.SUSPENDED
    assert runtime.get_proposals()[0].decision is ProposalDecision.REVIEW
    assert runtime.state_store.get("D1_CD", "target") == 48  # unchanged
    runtime.close()


async def test_llm_cannot_write_state_without_apply_pipeline(tmp_path):
    # Register the LLM interpreter but NOT apply_state_delta.  A high-confidence
    # ACCEPT still only produces a StateDelta + event; world state changes only
    # when the validated apply pipeline runs.
    runtime = Runtime(tmp_path / "bypass.db")
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.95, old=48))])
    bootstrap_llm_interpreter(runtime, backend)

    produced = await runtime.submit_event(Event("human_message", "user", {"text": "..."}))

    assert runtime.get_proposals()[0].decision is ProposalDecision.ACCEPT
    assert len(runtime.state_delta_store.all()) == 1
    assert "state_delta_created" in [e.type for e in produced]
    # No apply_state_delta registered -> world state was NOT written by the LLM.
    assert runtime.state_store.get("D1_CD", "target") is None
    runtime.close()
