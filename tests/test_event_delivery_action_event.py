"""AT13 (spec §64, §36): an action result is never lost, and never repeated.

Two guarantees have to hold at once here, and they pull in opposite directions:

* the ``action_succeeded`` event must reach the process waiting on it, even if
  the runtime dies right after the file was written;
* re-delivering that event must not cause the action to run a second time.

The first comes from the delivery obligation, the second from Phase 3C's
idempotency key.  Neither subsumes the other.
"""

from __future__ import annotations

from action_helpers import do_action, file_runtime, proposer_instance

from nexus_seed.actions.models import ActionExecutionStatus, ActionProposalStatus
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


async def test_the_result_event_carries_a_delivery_obligation(tmp_path):
    runtime = Runtime(tmp_path / "a.db")
    file_runtime(runtime, tmp_path / "sandbox")

    await runtime.submit_event(do_action(backend="local_file", target="out.txt"))

    succeeded = runtime.event_store.by_type("action_succeeded")[0]
    delivery = runtime.get_event_delivery(succeeded.id)
    assert delivery is not None and delivery.status.value == "DELIVERED"
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_a_crash_before_routing_the_result_does_not_lose_it(tmp_path):
    """AT13: the side effect happened; the waiter must still be told."""
    db_path = tmp_path / "a.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    backend = file_runtime(runtime, root)

    # Drive the pipeline by hand and stop the moment the executor commits its
    # action_succeeded — the crash window this test is about.
    runtime.event_store.append(do_action(backend="local_file", target="out.txt"))
    for _ in range(6):
        runtime.dispatch_pending_events()
        instance = runtime.scheduler.next_runnable()
        if instance is None:
            break
        await runtime.executor.execute(instance)
        if runtime.event_store.by_type("action_succeeded"):
            break

    succeeded = runtime.event_store.by_type("action_succeeded")[0]
    assert (root / "out.txt").exists()
    assert runtime.get_event_delivery(succeeded.id).status.value == "PENDING"
    proposer = proposer_instance(runtime)
    assert proposer.status is ProcessStatus.SUSPENDED
    runtime.close()

    # --- restart: the result reaches the waiting process ---
    runtime2 = Runtime(db_path)
    backend2 = file_runtime(runtime2, root)
    assert runtime2.get_pending_event_delivery_count() >= 1

    await runtime2.run_pending()

    assert runtime2.get_event_delivery(succeeded.id).status.value == "DELIVERED"
    assert runtime2.process_store.get_instance(proposer.id).status is ProcessStatus.COMPLETED

    # And the action itself did not run again.
    proposal = runtime2.get_action_proposals()[0]
    executions = runtime2.get_action_executions(proposal.id)
    assert [e.status for e in executions] == [ActionExecutionStatus.SUCCEEDED]
    assert backend2.calls == []
    assert len(runtime2.event_store.by_type("action_succeeded")) == 1
    runtime2.close()


async def test_re_delivering_the_result_does_not_re_run_the_action(tmp_path):
    """Delivery retry and action idempotency are separate, and both hold."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "a.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(do_action(backend="local_file", target="out.txt"))
    proposal = runtime.get_action_proposals()[0]
    calls_before = len(backend.calls)

    # Force the approval event back to PENDING and sweep again.
    approved = runtime.event_store.by_type("action_approved")[0]
    runtime.db.execute(
        "UPDATE event_deliveries SET status = 'PENDING' WHERE event_id = ?",
        (str(approved.id),),
    )
    await runtime.run_pending()

    assert len(backend.calls) == calls_before
    assert runtime.get_action_proposal(proposal.id).status is ActionProposalStatus.SUCCEEDED
    assert len(runtime.event_store.by_type("action_succeeded")) == 1
    runtime.close()


async def test_a_rejected_action_event_is_delivered_too(tmp_path):
    runtime = Runtime(tmp_path / "a.db")
    file_runtime(runtime, tmp_path / "sandbox", permissions=("filesystem.read",))

    await runtime.submit_event(
        do_action(backend="local_file", target="denied.txt",
                  required_permissions=["filesystem.write"])
    )

    rejected = runtime.event_store.by_type("action_rejected")[0]
    assert runtime.get_event_delivery(rejected.id).status.value == "DELIVERED"
    assert proposer_instance(runtime).status is ProcessStatus.COMPLETED
    runtime.close()
