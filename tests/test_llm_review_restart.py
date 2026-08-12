"""AT5: a REVIEW survives a full runtime restart and resumes on approval."""

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


async def test_review_survives_restart(tmp_path):
    db_path = tmp_path / "review_restart.db"

    # --- Runtime #1: medium confidence -> REVIEW -> suspend. ---
    backend = FakeLLMBackend(script=[proposal_response(proposal_dict(0.70))])
    runtime = Runtime(db_path)
    bootstrap_semantic(runtime)
    bootstrap_llm_interpreter(runtime, backend)
    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "interpret_event_llm"
    ][0]
    proposal_id = runtime.get_proposals()[0].id
    assert instance.status is ProcessStatus.SUSPENDED
    runtime.close()

    # --- Runtime #2: rebuilt from SQLite; handlers/backend re-registered. ---
    runtime2 = Runtime(db_path)
    bootstrap_semantic(runtime2)
    bootstrap_llm_interpreter(runtime2, FakeLLMBackend())  # backend not needed on resume
    assert runtime2.process_store.get_instance(instance.id).status is ProcessStatus.SUSPENDED

    await runtime2.submit_event(
        Event("interpretation_reviewed", "human", {"proposal_id": str(proposal_id), "decision": "approve"})
    )

    assert runtime2.process_store.get_instance(instance.id).status is ProcessStatus.COMPLETED
    assert runtime2.get_proposal(proposal_id).decision is ProposalDecision.ACCEPT
    assert runtime2.state_store.get("D1_CD", "target") == 45
    runtime2.close()
