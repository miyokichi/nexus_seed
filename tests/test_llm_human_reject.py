"""AT6: a human rejection ends the process with no world change."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.intelligence.proposal import ProposalDecision
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


def proposal_dict(confidence):
    return {
        "subject": "D1_CD",
        "predicate": "target_changed",
        "confidence": confidence,
        "rationale": "nm change",
        "proposed_state_deltas": [
            {"entity": "D1_CD", "attribute": "target", "old_value": 48, "new_value": 45, "unit": "nm", "confidence": confidence}
        ],
    }


async def test_human_reject_leaves_world_unchanged(tmp_path):
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.70))])
    runtime = Runtime(tmp_path / "reject.db")
    bootstrap_semantic(runtime)
    bootstrap_llm_interpreter(runtime, backend)

    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))
    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "interpret_event_llm"
    ][0]
    proposal = runtime.get_proposals()[0]

    produced = await runtime.submit_event(
        Event(
            "interpretation_reviewed",
            "human",
            {"proposal_id": str(proposal.id), "decision": "reject"},
        )
    )

    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.COMPLETED
    assert runtime.get_proposal(proposal.id).decision is ProposalDecision.REJECT
    assert runtime.state_store.get("D1_CD", "target") is None
    assert runtime.observation_store.all() == []
    assert runtime.state_delta_store.all() == []
    assert "interpretation_rejected" in [e.type for e in produced]
    runtime.close()
