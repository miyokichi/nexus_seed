"""AT11 + AT12 + AT13 (spec §60, §61, §62): results come back as Events, and
every act is traceable to the message that caused it.

Two invariants meet here.  A backend result is *reported*, never *applied*
(Invariant 24/25): it lands in the ActionExecution journal and in an event
payload, and world state is left for the ordinary Observation -> StateDelta
path.  And the whole outbound chain resolves backwards from storage alone.
"""

from __future__ import annotations

import uuid

from action_helpers import (
    analysis_proposal,
    do_action,
    fake_runtime,
    full_stack,
    human_message,
    instances_named,
)

from nexus_seed.actions.models import ActionExecutionStatus
from nexus_seed.backends import action_success, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.runtime.runtime import Runtime


async def test_exactly_one_result_event_and_it_reaches_the_execution(tmp_path):
    """AT11: one action_succeeded, and the journal is one hop away from it."""
    runtime = Runtime(tmp_path / "result.db")
    fake_runtime(runtime)

    await runtime.submit_event(do_action(target="out.txt"))

    events = runtime.event_store.by_type("action_succeeded")
    assert len(events) == 1
    payload = events[0].payload

    execution = runtime.action_execution_store.get(
        uuid.UUID(payload["action_execution_id"])
    )
    assert execution.status is ActionExecutionStatus.SUCCEEDED
    assert str(execution.action_proposal_id) == payload["action_proposal_id"]
    assert payload["backend"] == "fake_action"
    assert payload["action_type"] == "write_file"
    runtime.close()


async def test_backend_result_does_not_touch_world_state(tmp_path):
    """AT12: a result that *looks like* a state fact still is not one."""
    runtime = Runtime(tmp_path / "nostate.db")
    fake_runtime(runtime, script=[action_success({"D1_CD": 42})])

    await runtime.submit_event(do_action(target="out.txt"))

    # The backend reported a number that names a real entity.  It changed
    # nothing: reaching world state requires an Event -> Observation ->
    # StateDelta path that nothing here travelled.
    assert runtime.state_store.get("D1_CD", "target") is None
    assert runtime.state_store.get("D1_CD", "D1_CD") is None
    assert runtime.state_store.all_current() == []
    assert runtime.observation_store.all() == []
    assert runtime.state_delta_store.all() == []

    # It is not lost, though — it is in the journal and in the event payload.
    proposal = runtime.get_action_proposals()[0]
    assert runtime.get_action_executions(proposal.id)[0].result == {"D1_CD": 42}
    assert runtime.event_store.by_type("action_succeeded")[0].payload["result"] == {
        "D1_CD": 42
    }
    runtime.close()


async def test_action_traces_back_to_the_raw_message(tmp_path):
    """AT13: execution -> proposal -> process -> work -> delta -> observation -> event."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "trace.db")
    full_stack(runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))])

    await runtime.submit_event(human_message())

    proposal = runtime.get_action_proposals()[0]
    trace = runtime.get_action_trace(proposal.id)

    assert trace.proposal.id == proposal.id
    assert trace.succeeded_execution is not None
    assert trace.succeeded_execution.status is ActionExecutionStatus.SUCCEEDED

    worker = instances_named(runtime, "write_analysis_result")[0]
    assert trace.process_instance.id == worker.id

    assert trace.work_requirement.work_type == "write_analysis_result"
    assert trace.state_delta.entity == "D1_CD"
    assert trace.state_delta.attribute == "analysis_result"
    assert trace.observation.subject == "D1_CD"
    assert trace.observation.predicate == "analysis_completed"

    # The far end of the chain is the human's original message.
    assert trace.source_event.type == "human_message"
    assert trace.source_event.payload["text"].startswith("D1の")

    # And the authorization is on the same trace.
    assert [d.decision.value for d in trace.decisions] == ["APPROVE"]
    assert trace.decisions[0].granted_permissions == ["filesystem.write"]
    runtime.close()


async def test_human_approval_is_part_of_the_trace(tmp_path):
    """AT (spec §48): who let this happen is answerable from the trace."""
    runtime = Runtime(tmp_path / "approval_trace.db")
    fake_runtime(runtime)

    await runtime.submit_event(do_action(target="risky.txt", risk_level="HIGH"))
    proposal_id = runtime.get_action_proposals()[0].id
    await runtime.submit_event(
        Event(
            "action_reviewed",
            "alex@example.test",
            {"proposal_id": str(proposal_id), "decision": "approve"},
        )
    )

    trace = runtime.get_action_trace(proposal_id)
    assert len(trace.review_events) == 1
    assert trace.review_events[0].source == "alex@example.test"
    assert [d.decision.value for d in trace.decisions] == ["REVIEW", "APPROVE"]
    assert trace.decisions[1].reviewed_by_event_id == trace.review_events[0].id
    runtime.close()


async def test_trace_of_an_unknown_proposal_is_none(tmp_path):
    runtime = Runtime(tmp_path / "empty.db")
    assert runtime.get_action_trace(uuid.uuid4()) is None
    runtime.close()
