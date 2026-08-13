"""Spec §69: a backend call that failed must still be visible in the log.

The Phase 3B gap: an LLM call that timed out or returned unusable output was
rolled back with the retry, taking its ``llm_invocations`` row with it — so the
audit log recorded only the calls that went well.  Phase 3C treats invocations
as an attempt *journal*, on the same footing as ActionExecution.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.backends import (
    FakeLLMBackend,
    failure_response,
    invalid_response,
    proposal_response,
)
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
            {
                "entity": "D1_CD",
                "attribute": "target",
                "old_value": 48,
                "new_value": 45,
                "unit": "nm",
                "confidence": confidence,
            }
        ],
    }


def interpreter(runtime):
    return [
        i
        for i in runtime.process_store.all_instances()
        if i.definition_name == "interpret_event_llm"
    ][0]


async def test_a_failed_backend_call_is_recorded(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "invoke.db", clock=clock)
    bootstrap_semantic(runtime)
    bootstrap_llm_interpreter(
        runtime,
        FakeLLMBackend(script=[failure_response("timeout"), proposal_response(proposal_dict(0.95))]),
    )

    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))
    instance = interpreter(runtime)
    assert instance.status is ProcessStatus.RETRY_WAIT

    # The failed attempt is durable *before* the retry, not only after success.
    invocations = runtime.get_llm_invocations(instance.id)
    assert len(invocations) == 1
    assert invocations[0].success is False
    assert invocations[0].error == "timeout"

    clock.advance(10)
    await runtime.tick()

    invocations = runtime.get_llm_invocations(instance.id)
    assert [i.success for i in invocations] == [False, True]
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()


async def test_schema_failures_are_recorded_on_every_attempt(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "schema.db", clock=clock)
    bootstrap_semantic(runtime)
    bootstrap_llm_interpreter(runtime, FakeLLMBackend(script=[invalid_response()]))

    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))
    instance = interpreter(runtime)

    for _ in range(2):
        clock.advance(10)
        await runtime.tick()

    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.FAILED

    # Three attempts, all recorded, all marked unsuccessful with a reason.
    invocations = runtime.get_llm_invocations(instance.id)
    assert len(invocations) == 3
    assert all(i.success is False for i in invocations)
    assert all("schema validation failed" in (i.error or "") for i in invocations)

    # And still nothing partial reached the world model.
    assert runtime.get_proposals() == []
    assert runtime.observation_store.all() == []
    assert runtime.state_delta_store.all() == []
    runtime.close()


async def test_a_successful_call_still_links_to_its_proposal(tmp_path):
    """The fix must not disturb the Phase 3B provenance chain."""
    runtime = Runtime(tmp_path / "ok.db")
    bootstrap_semantic(runtime)
    bootstrap_llm_interpreter(
        runtime, FakeLLMBackend(script=[proposal_response(proposal_dict(0.95))])
    )

    await runtime.submit_event(Event("human_message", "user", {"text": "..."}))

    proposal = runtime.get_proposals()[0]
    invocation = runtime.get_llm_invocation(proposal.llm_invocation_id)
    assert invocation is not None and invocation.success is True
    assert invocation.context_snapshot_id == proposal.context_snapshot_id
    runtime.close()
