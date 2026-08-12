"""AT9 + AT10: invalid output and backend failure use the Phase 2A retry loop."""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.backends import FakeLLMBackend, failure_response, invalid_response, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.llm_interpret import bootstrap_llm_interpreter
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.clock import ManualClock
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


async def test_backend_failure_then_success(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "retry.db", clock=clock)
    bootstrap_semantic(runtime)
    backend = FakeLLMBackend(script=[failure_response("timeout"), proposal_response(proposal_dict(0.95))])
    bootstrap_llm_interpreter(runtime, backend)

    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))
    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "interpret_event_llm"
    ][0]
    assert instance.status is ProcessStatus.RETRY_WAIT

    clock.advance(10)
    await runtime.tick()

    final = runtime.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.COMPLETED
    assert runtime.state_store.get("D1_CD", "target") == 45


async def test_invalid_schema_exhausts_retries_then_fails(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "invalid.db", clock=clock)
    bootstrap_semantic(runtime)
    backend = FakeLLMBackend(script=[invalid_response()])  # always invalid
    bootstrap_llm_interpreter(runtime, backend)

    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))
    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "interpret_event_llm"
    ][0]
    assert instance.status is ProcessStatus.RETRY_WAIT

    # max_retries == 2 -> two more attempts, then FAILED.
    clock.advance(10)
    await runtime.tick()
    clock.advance(10)
    await runtime.tick()

    final = runtime.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.FAILED
    # No partial proposal / observation / delta was persisted.
    assert runtime.get_proposals() == []
    assert runtime.observation_store.all() == []
    assert runtime.state_delta_store.all() == []
    runtime.close()
