"""AT20 (spec §71, §33–§35): delivery joins the existing traces.

"Where did this come from?" was already answerable.  Phase 3F adds the missing
step in the middle — *was this event actually handed to anyone, when, and after
how many tries* — so a stalled system can be diagnosed instead of guessed at.
"""

from __future__ import annotations

from delivery_helpers import break_router, heal_router, manual_runtime, ping, register_noter
from resource_helpers import FACT_DOCUMENT, full_stack, watched_tree, write_file

from nexus_seed.delivery.models import EventDeliveryStatus
from nexus_seed.runtime.runtime import Runtime


async def test_a_delivery_record_explains_a_settled_event(tmp_path):
    runtime = Runtime(tmp_path / "t.db")
    register_noter(runtime)
    event = ping()
    await runtime.submit_event(event)

    delivery = runtime.get_event_delivery(event.id)
    assert delivery.event_id == event.id
    assert delivery.status is EventDeliveryStatus.DELIVERED
    assert delivery.attempt_count == 1
    assert delivery.last_error is None
    assert delivery.delivered_at is not None
    runtime.close()


async def test_a_delivery_record_explains_a_stuck_event(tmp_path):
    """The diagnosis an operator actually needs."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=2)

    event = ping()
    await runtime.submit_event(event)

    delivery = runtime.get_event_delivery(event.id)
    assert delivery.status is EventDeliveryStatus.RETRY_WAIT
    assert delivery.attempt_count == 1
    assert "router exploded" in delivery.last_error
    assert delivery.next_attempt_at > clock.now()
    assert delivery.delivered_at is None
    runtime.close()


async def test_an_unknown_event_has_no_delivery_record(tmp_path):
    import uuid

    runtime = Runtime(tmp_path / "t.db")
    assert runtime.get_event_delivery(uuid.uuid4()) is None
    runtime.close()


async def test_delivery_joins_the_ingress_and_action_traces(tmp_path):
    """One chain: external source -> ingress -> delivery -> work -> action."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "t.db")
    adapter, backend = full_stack(runtime, root, out)

    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()

    file_event = runtime.event_store.by_type("file_created")[0]

    # Ingress: which delivery from the world produced this event.
    ingress_trace = runtime.get_ingress_trace(file_event.id)
    assert ingress_trace.source_identity[0] == "local_file"

    # Delivery: whether that event was actually handed on, and when.
    delivery = runtime.get_event_delivery(file_event.id)
    assert delivery.status is EventDeliveryStatus.DELIVERED
    assert delivery.delivered_at >= ingress_trace.receipt.received_at

    # Action: what it eventually caused in the world.
    proposal = runtime.get_action_proposals()[0]
    action_trace = runtime.get_action_trace(proposal.id)
    assert action_trace.succeeded_execution is not None
    assert runtime.get_event_delivery(action_trace.source_event.id).status is (
        EventDeliveryStatus.DELIVERED
    )
    runtime.close()


async def test_health_distinguishes_pending_from_waiting_from_failed(tmp_path):
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    break_router(runtime, failures=99, only_type="poison")

    from nexus_seed.core.event import Event

    runtime.event_store.append(Event("poison", "test", {}))
    await runtime.run_pending()
    runtime.event_store.append(ping())

    health = runtime.get_delivery_health()
    assert health["retry_wait"] == 1
    assert health["pending"] == 1
    assert health["failed"] == 0
    assert health["outstanding"] == 2
    assert health["oldest_pending_age_seconds"] is not None

    heal_router(runtime)
    clock.advance(600)
    await runtime.tick()

    assert runtime.get_delivery_health()["outstanding"] == 0
    runtime.close()


async def test_failed_deliveries_are_listable_for_a_human(tmp_path):
    """Spec §78: no DLQ machinery, but a failure must be findable."""
    import uuid

    runtime = Runtime(tmp_path / "t.db")
    ghost = uuid.uuid4()
    runtime.event_delivery_store.create_for_event(ghost)
    runtime.dispatch_pending_events()

    failed = runtime.get_failed_event_deliveries()
    assert [d.event_id for d in failed] == [ghost]
    assert failed[0].last_error == "event not found"
    runtime.close()


async def test_deliveries_can_be_filtered_by_status(tmp_path):
    runtime = Runtime(tmp_path / "t.db")
    register_noter(runtime)
    await runtime.submit_event(ping())
    runtime.event_store.append(ping({"n": 2}))

    assert len(runtime.get_event_deliveries()) == 2
    assert len(runtime.get_event_deliveries(EventDeliveryStatus.DELIVERED)) == 1
    assert len(runtime.get_event_deliveries("PENDING")) == 1
    runtime.close()
