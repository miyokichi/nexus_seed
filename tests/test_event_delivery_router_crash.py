"""AT4 + AT6 (spec §55, §57): routing and acknowledgement are one transaction.

The dangerous middle state is "processes were created but the event is not
marked delivered": a re-dispatch would then start them again.  Committing the
acknowledgement *with* the activations removes the state entirely — and a
separate routing-idempotency guard covers the case anyway.
"""

from __future__ import annotations

from delivery_helpers import (
    RECORDED,
    instances_named,
    manual_runtime,
    noted_definition,
    noter,
    ping,
    register_noter,
    status_of,
)

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessInstance, ProcessStatus


class HalfRouter:
    """Creates activations, then fails before the acknowledgement can commit."""

    def __init__(self, real, process_store, definition_name="noter") -> None:
        self.real = real
        self.process_store = process_store
        self.definition_name = definition_name
        self.armed = True

    def route(self, event):
        if not self.armed:
            return self.real.route(event)
        self.armed = False
        # Do the work the real router would have done...
        self.process_store.save_instance(
            ProcessInstance(
                definition_name=self.definition_name,
                definition_version="1",
                status=ProcessStatus.RUNNABLE,
                input={"trigger_event_id": str(event.id), "payload": event.payload},
                pending_event_id=event.id,
                trigger_event_id=event.id,
            )
        )
        # ...and then die before the delivery can be acknowledged.
        raise RuntimeError("crashed after creating activations")


async def test_activations_roll_back_with_the_acknowledgement(tmp_path):
    """AT6: a crash between routing and acknowledging leaves neither."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    runtime.dispatcher.router = HalfRouter(runtime.router, runtime.process_store)

    event = ping()
    await runtime.submit_event(event)

    # The instance the half-router created was rolled back with the failure.
    assert instances_named(runtime, "noter") == []
    assert status_of(runtime, event.id) == "RETRY_WAIT"
    assert RECORDED == []

    clock.advance(60)
    await runtime.tick()

    # The retry produced exactly one process, not a second one.
    assert len(instances_named(runtime, "noter")) == 1
    assert len(RECORDED) == 1
    assert status_of(runtime, event.id) == "DELIVERED"
    runtime.close()


async def test_a_surviving_activation_is_not_duplicated_by_a_retry(tmp_path):
    """The routing idempotency guard, tested independently of the transaction.

    Simulates the state the atomic commit is designed to prevent — an
    activation on disk with the delivery still outstanding — and shows the
    re-dispatch recognises it rather than starting a second copy (spec §16).
    """
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)

    event = ping()
    runtime.event_store.append(event)
    # An activation from an attempt whose acknowledgement never landed.
    runtime.process_store.save_instance(
        ProcessInstance(
            definition_name="noter",
            definition_version="1",
            status=ProcessStatus.COMPLETED,
            input={"trigger_event_id": str(event.id), "payload": {}},
            trigger_event_id=event.id,
        )
    )

    await runtime.run_pending()

    assert len(instances_named(runtime, "noter")) == 1
    assert status_of(runtime, event.id) == "DELIVERED"
    runtime.close()


async def test_a_router_failure_leaves_no_half_delivered_state(tmp_path):
    """AT4: after a failed attempt the event is exactly where it started."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)
    runtime.dispatcher.router = HalfRouter(runtime.router, runtime.process_store)

    event = ping()
    await runtime.submit_event(event)

    delivery = runtime.get_event_delivery(event.id)
    assert delivery.outstanding
    assert delivery.attempt_count == 1
    assert "crashed after creating activations" in delivery.last_error
    assert delivery.delivered_at is None
    assert runtime.event_store.get(event.id) is not None
    runtime.close()


async def test_two_definitions_on_one_event_both_start_exactly_once(tmp_path):
    """A partially-routed event must not double up the definitions it reached."""
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime, "noter")  # also clears the recording buffer
    runtime.register_process(noted_definition("second"), noter)

    event = ping()
    await runtime.submit_event(event)

    assert len(instances_named(runtime, "noter")) == 1
    assert len(instances_named(runtime, "second")) == 1

    # Force a re-dispatch of the same event.
    runtime.db.execute(
        "UPDATE event_deliveries SET status = 'PENDING' WHERE event_id = ?",
        (str(event.id),),
    )
    await runtime.run_pending()

    assert len(instances_named(runtime, "noter")) == 1
    assert len(instances_named(runtime, "second")) == 1
    runtime.close()


async def test_an_unknown_event_type_is_delivered_without_incident(tmp_path):
    runtime, clock = manual_runtime(tmp_path)
    register_noter(runtime)

    event = Event("nothing_listens_to_this", "test", {})
    await runtime.submit_event(event)

    assert status_of(runtime, event.id) == "DELIVERED"
    assert RECORDED == []
    runtime.close()
