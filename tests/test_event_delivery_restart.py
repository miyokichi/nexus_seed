"""AT5 + AT9 (spec §56, §60): a stored event outlives the runtime that stored it.

The central Phase 3F claim, tested at its narrowest: the event committed, the
runtime died before routing it, and nothing upstream will offer it again.  The
restart still routes it, because the *obligation* was committed alongside the
event (Invariant 44).
"""

from __future__ import annotations

from delivery_helpers import (
    RECORDED,
    break_router,
    instances_named,
    manual_runtime,
    ping,
    register_noter,
    status_of,
)

from nexus_seed.delivery.models import EventDeliveryStatus
from nexus_seed.runtime.runtime import Runtime


async def test_an_unrouted_event_is_picked_up_after_a_restart(tmp_path):
    """AT5."""
    db_path = tmp_path / "restart.db"

    runtime = Runtime(db_path)
    register_noter(runtime)
    event = ping({"n": 1})
    # Persisted with its obligation; the runtime dies before draining.
    runtime.event_store.append(event)
    assert status_of(runtime, event.id) == "PENDING"
    assert runtime.process_store.all_instances() == []
    runtime.close()

    runtime2 = Runtime(db_path)
    register_noter(runtime2)
    assert runtime2.get_pending_event_delivery_count() == 1

    await runtime2.run_pending()

    assert status_of(runtime2, event.id) == "DELIVERED"
    assert len(instances_named(runtime2, "noter")) == 1
    assert RECORDED[0]["event_id"] == str(event.id)
    runtime2.close()


async def test_a_stale_delivering_claim_is_recovered(tmp_path):
    """AT9: an interrupted attempt is visible and safe to redo."""
    db_path = tmp_path / "restart.db"

    runtime = Runtime(db_path)
    register_noter(runtime)
    event = ping()
    runtime.event_store.append(event)
    # Claimed, then the process died mid-route.
    runtime.event_delivery_store.mark_delivering(event.id)
    assert status_of(runtime, event.id) == "DELIVERING"
    runtime.close()

    runtime2 = Runtime(db_path)
    register_noter(runtime2)

    # Startup recovery returned the claim to PENDING (before any sweep ran).
    assert status_of(runtime2, event.id) == "PENDING"
    assert runtime2.get_event_delivery(event.id).attempt_count == 1

    await runtime2.run_pending()

    assert status_of(runtime2, event.id) == "DELIVERED"
    assert len(instances_named(runtime2, "noter")) == 1
    assert runtime2.get_event_delivery(event.id).attempt_count == 2
    runtime2.close()


async def test_a_retry_wait_delivery_resumes_after_a_restart(tmp_path):
    """The backoff schedule is durable, not in-memory."""
    db_path = tmp_path / "restart.db"

    runtime, clock = manual_runtime(tmp_path, "restart.db")
    register_noter(runtime)
    break_router(runtime, failures=1)
    event = ping()
    await runtime.submit_event(event)
    assert status_of(runtime, event.id) == "RETRY_WAIT"
    runtime.close()

    runtime2 = Runtime(db_path, clock=clock)
    register_noter(runtime2)
    assert runtime2.get_event_delivery(event.id).status is EventDeliveryStatus.RETRY_WAIT

    clock.advance(60)
    await runtime2.tick()

    assert status_of(runtime2, event.id) == "DELIVERED"
    assert len(instances_named(runtime2, "noter")) == 1
    runtime2.close()


async def test_several_outstanding_events_are_all_recovered_in_order(tmp_path):
    db_path = tmp_path / "restart.db"

    runtime = Runtime(db_path)
    register_noter(runtime)
    events = [ping({"n": n}) for n in range(3)]
    for event in events:
        runtime.event_store.append(event)
    runtime.close()

    runtime2 = Runtime(db_path)
    register_noter(runtime2)
    assert runtime2.get_pending_event_delivery_count() == 3

    await runtime2.run_pending()

    assert [r["event_id"] for r in RECORDED] == [str(e.id) for e in events]
    assert runtime2.get_pending_event_delivery_count() == 0
    runtime2.close()


async def test_already_delivered_events_are_not_re_routed_on_restart(tmp_path):
    """Restart recovery is not replay (spec §28)."""
    db_path = tmp_path / "restart.db"

    runtime = Runtime(db_path)
    register_noter(runtime)
    await runtime.submit_event(ping())
    assert len(instances_named(runtime, "noter")) == 1
    runtime.close()

    runtime2 = Runtime(db_path)
    register_noter(runtime2)
    await runtime2.run_pending()

    assert len(instances_named(runtime2, "noter")) == 1
    assert RECORDED == []  # nothing ran in the second runtime
    runtime2.close()


async def test_recovery_runs_before_any_work_and_needs_no_prompting(tmp_path):
    """The sweep is part of startup, not something a caller must remember."""
    db_path = tmp_path / "restart.db"

    runtime = Runtime(db_path)
    event = ping()
    runtime.event_store.append(event)
    runtime.event_delivery_store.mark_delivering(event.id)
    runtime.close()

    runtime2 = Runtime(db_path)
    # No run_pending yet — construction alone reconciled the claim.
    assert status_of(runtime2, event.id) == "PENDING"
    runtime2.close()
