"""AT1: a high-confidence proposal is accepted and flows to world state."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.intelligence.proposal import ProposalDecision
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


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


MESSAGE = "D1のCD targetを48nmから45nmへ変更しました。"


async def test_high_confidence_accepts_and_updates_world(tmp_path):
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.95))])
    runtime = Runtime(tmp_path / "hc.db")
    bootstrap_semantic(runtime)  # provides apply_state_delta
    bootstrap_llm_interpreter(runtime, backend)

    await runtime.submit_event(Event("human_message", "user", {"text": MESSAGE}))

    # Proposal ACCEPTed.
    proposals = runtime.get_proposals()
    assert len(proposals) == 1
    assert proposals[0].decision is ProposalDecision.ACCEPT

    # Observation carries the proposal id; a StateDelta was produced.
    observations = runtime.observation_store.all()
    assert len(observations) == 1
    assert observations[0].proposal_id == proposals[0].id
    assert len(runtime.state_delta_store.all()) == 1

    # World state updated through the existing apply pipeline.
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()
