"""Phase 3B integration (§54, §55): LLM interpretation drives the full loop.

Scenario A: a high-confidence natural-language event flows all the way through
the existing semantic + work-intelligence pipeline, suspends for a measurement,
survives a runtime restart, resumes, and satisfies the work — proving the LLM
boundary reuses every existing mechanism.

Scenario B: a medium-confidence event goes to human review, survives a restart,
and applies on approval.
"""

from __future__ import annotations

from nexus_seed.backends import FakeLLMBackend, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


def proposal_dict(confidence):
    return {
        "subject": "D1_CD",
        "predicate": "target_changed",
        "confidence": confidence,
        "rationale": "message reports an nm target change",
        "proposed_state_deltas": [
            {"entity": "D1_CD", "attribute": "target", "old_value": 48, "new_value": 45, "unit": "nm", "confidence": confidence}
        ],
    }


MESSAGE = "D1のCD targetを48nmから45nmへ変更しました。抵抗への影響を再確認してください。"


def _bootstrap(runtime, backend):
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    bootstrap_llm_interpreter(runtime, backend)


async def test_high_confidence_drives_work_across_restart(tmp_path):
    db_path = tmp_path / "wi.db"

    runtime = Runtime(db_path)
    _bootstrap(runtime, FakeLLMBackend(script=[proposal_response(proposal_dict(0.95))]))
    await runtime.submit_event(Event("human_message", "user", {"text": MESSAGE}))

    # LLM ACCEPT -> world state -> work spawned -> resistance_check suspended.
    assert runtime.state_store.get("D1_CD", "target") == 45
    resistance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "resistance_check"
    ][0]
    assert resistance.status is ProcessStatus.SUSPENDED
    req = runtime.get_work_requirement(resistance.work_requirement_id)
    assert req.status is WorkStatus.SPAWNED
    runtime.close()

    # Restart, deliver the measurement, resume to completion.
    runtime2 = Runtime(db_path)
    _bootstrap(runtime2, FakeLLMBackend())
    await runtime2.submit_event(
        Event("measurement_completed", "metrology", {"wafer": "W03", "resistance": 123.4})
    )

    assert runtime2.process_store.get_instance(resistance.id).status is ProcessStatus.COMPLETED
    assert runtime2.get_work_requirement(req.id).status is WorkStatus.SATISFIED

    # The whole chain traces back to the human message.
    trace = runtime2.get_work_trace(req.id)
    assert trace.source_event.type == "human_message"
    runtime2.close()


async def test_review_then_approve_integration(tmp_path):
    db_path = tmp_path / "review_wi.db"

    runtime = Runtime(db_path)
    _bootstrap(runtime, FakeLLMBackend(script=[proposal_response(proposal_dict(0.70))]))
    await runtime.submit_event(Event("human_message", "user", {"text": MESSAGE}))

    interp = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "interpret_event_llm"
    ][0]
    assert interp.status is ProcessStatus.SUSPENDED
    proposal_id = runtime.get_proposals()[0].id
    assert runtime.state_store.get("D1_CD", "target") is None
    runtime.close()

    # Restart, then a human approves.
    runtime2 = Runtime(db_path)
    _bootstrap(runtime2, FakeLLMBackend())
    await runtime2.submit_event(
        Event("interpretation_reviewed", "human", {"proposal_id": str(proposal_id), "decision": "approve"})
    )

    assert runtime2.process_store.get_instance(interp.id).status is ProcessStatus.COMPLETED
    assert runtime2.state_store.get("D1_CD", "target") == 45
    # Approval flowed into the work pipeline too.
    resistance = [
        i for i in runtime2.process_store.all_instances() if i.definition_name == "resistance_check"
    ]
    assert len(resistance) == 1
    runtime2.close()
