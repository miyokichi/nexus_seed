"""AT11: trace a world value back through the full LLM interpretation chain."""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.core.event import Event
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


async def test_full_llm_provenance_chain(tmp_path):
    runtime = Runtime(tmp_path / "trace.db")
    bootstrap_semantic(runtime)
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.95))])
    bootstrap_llm_interpreter(runtime, backend)

    raw = Event("human_message", "user", {"text": "D1のCD targetを48nmから45nmへ変更"})
    await runtime.submit_event(raw)

    # World State -> StateDelta -> Observation -> Proposal -> Invocation
    #             -> Context Snapshot -> Raw Event
    current = runtime.get_current_state("D1_CD", "target")
    assert current.value == 45
    assert current.state_delta_id is not None

    delta = runtime.state_delta_store.get(current.state_delta_id)
    assert delta is not None
    observation = runtime.observation_store.get(delta.observation_id)
    assert observation is not None
    assert observation.proposal_id is not None

    proposal = runtime.get_proposal(observation.proposal_id)
    assert proposal is not None
    assert proposal.llm_invocation_id is not None

    invocation = runtime.get_llm_invocation(proposal.llm_invocation_id)
    assert invocation is not None
    assert invocation.backend == "llm"
    assert invocation.context_snapshot_id is not None

    snapshot = runtime.context_snapshot_store.get(invocation.context_snapshot_id)
    assert snapshot is not None

    source_event = runtime.event_store.get(observation.source_event_id)
    assert source_event is not None
    assert source_event.id == raw.id
    assert source_event.type == "human_message"
    runtime.close()
