"""AT8 (spec §57): backend failure retries, and every attempt stays on record.

Two things are being proven at once:

* retries reuse the Phase 2A mechanism — the action subsystem has no retry loop
  of its own (spec §32);
* the *failed* attempt survives the rollback that a retry causes.  A journal
  that only records successes would be useless for exactly the incident it is
  meant to explain (Invariant 26).
"""

from __future__ import annotations

from datetime import datetime, timezone

from action_helpers import do_action, fake_runtime, instances_named, proposer_instance

from nexus_seed.actions.models import ActionExecutionStatus, ActionProposalStatus
from nexus_seed.backends import action_failure, action_success, action_timeout
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


async def test_failure_then_success_records_both_attempts(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "retry.db", clock=clock)
    backend = fake_runtime(runtime, script=[action_timeout("connection reset"), action_success()])

    await runtime.submit_event(do_action(target="flaky.txt"))

    proposal_id = runtime.get_action_proposals()[0].id
    executor = instances_named(runtime, "action_executor")[0]
    assert executor.status is ProcessStatus.RETRY_WAIT

    # Attempt 1 failed and is already durable, before any retry happens.
    first = runtime.get_action_executions(proposal_id)
    assert [(e.attempt, e.status) for e in first] == [(1, ActionExecutionStatus.FAILED)]
    assert first[0].error == "connection reset"

    clock.advance(10)
    await runtime.tick()

    executions = runtime.get_action_executions(proposal_id)
    assert [(e.attempt, e.status) for e in executions] == [
        (1, ActionExecutionStatus.FAILED),
        (2, ActionExecutionStatus.SUCCEEDED),
    ]
    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.SUCCEEDED
    assert runtime.process_store.get_instance(executor.id).status is ProcessStatus.COMPLETED
    assert proposer_instance(runtime).status is ProcessStatus.COMPLETED
    # The effect happened exactly once despite two backend calls.
    assert backend.effect_count == 1
    runtime.close()


async def test_exhausted_retries_report_action_failed(tmp_path):
    """When the budget runs out the action fails as an *event*, not a hang."""
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "exhaust.db", clock=clock)
    backend = fake_runtime(runtime, script=[action_timeout("still down")])

    await runtime.submit_event(do_action(target="down.txt"))
    proposal_id = runtime.get_action_proposals()[0].id

    # max_retries == 2 -> attempts 2 and 3.
    for _ in range(2):
        clock.advance(30)
        await runtime.tick()

    executions = runtime.get_action_executions(proposal_id)
    assert [e.attempt for e in executions] == [1, 2, 3]
    assert all(e.status is ActionExecutionStatus.FAILED for e in executions)
    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.FAILED
    assert backend.effect_count == 0

    failed_events = runtime.event_store.by_type("action_failed")
    assert len(failed_events) == 1
    assert failed_events[0].payload["attempts"] == 3

    proposer = proposer_instance(runtime)
    assert proposer.status is ProcessStatus.COMPLETED
    assert proposer.local_state["output"]["outcome"] == "action_failed"
    runtime.close()


async def test_a_permanent_failure_is_not_retried(tmp_path):
    """A non-retryable backend result must not consume the retry budget."""
    runtime = Runtime(tmp_path / "permanent.db")
    backend = fake_runtime(
        runtime, script=[action_failure("unsupported", retryable=False)]
    )

    await runtime.submit_event(do_action(target="never.txt"))
    proposal_id = runtime.get_action_proposals()[0].id

    executions = runtime.get_action_executions(proposal_id)
    assert [e.attempt for e in executions] == [1]
    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.FAILED
    assert len(backend.calls) == 1
    runtime.close()


async def test_missing_backend_fails_without_calling_anything(tmp_path):
    runtime = Runtime(tmp_path / "nobackend.db")
    fake_runtime(runtime)

    await runtime.submit_event(do_action(backend="not_registered", target="x.txt"))

    proposal = runtime.get_action_proposals()[0]
    # Validation refuses it before execution: an unregistered backend publishes
    # no capabilities, so nothing can vouch for the action.
    assert proposal.status is ActionProposalStatus.REJECTED
    assert runtime.get_action_executions(proposal.id) == []
    runtime.close()
