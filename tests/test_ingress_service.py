"""AT1 + AT7 (spec §51, §57): the ingress service turns envelopes into Events.

The service is the only place an external occurrence becomes an Event
(Invariant 28), and it holds no knowledge of any particular source.
"""

from __future__ import annotations

from ingress_helpers import ingress, manual_envelope

from nexus_seed.ingress.models import IngressStatus
from nexus_seed.runtime.runtime import Runtime


async def test_a_manual_envelope_becomes_exactly_one_event(tmp_path):
    """AT1."""
    runtime = Runtime(tmp_path / "m.db")
    result = await ingress(runtime).ingest(
        manual_envelope(source_event_key="demo-001", payload={"text": "hello"})
    )

    assert result.status is IngressStatus.ACCEPTED
    assert result.accepted and not result.duplicate

    events = runtime.event_store.all()
    assert len(events) == 1
    event = events[0]
    assert event.type == "human_message"
    assert event.source == "manual"
    assert event.payload == {"text": "hello"}
    assert event.ingress_receipt_id == result.receipt.id
    runtime.close()


async def test_the_receipt_records_the_external_identity(tmp_path):
    runtime = Runtime(tmp_path / "m.db")
    result = await ingress(runtime).ingest(manual_envelope(source_event_key="demo-001"))

    receipt = runtime.get_ingress_receipt(result.receipt.id)
    assert receipt.adapter_id == "manual"
    assert receipt.source_event_key == "demo-001"
    assert receipt.status is IngressStatus.ACCEPTED
    assert receipt.event_id == result.event.id
    runtime.close()


async def test_the_payload_is_carried_through_untouched(tmp_path):
    """Ingress never interprets — the payload arrives as the adapter built it."""
    runtime = Runtime(tmp_path / "m.db")
    payload = {"text": "D1のCD", "nested": {"a": [1, 2]}, "n": 3}
    result = await ingress(runtime).ingest(manual_envelope(payload=payload))

    assert result.event.payload == payload
    assert runtime.event_store.get(result.event.id).payload == payload
    runtime.close()


async def test_observed_at_becomes_the_event_time(tmp_path):
    from datetime import datetime, timezone

    runtime = Runtime(tmp_path / "m.db")
    when = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)
    envelope = manual_envelope()
    envelope.observed_at = when

    result = await ingress(runtime).ingest(envelope)
    assert runtime.event_store.get(result.event.id).occurred_at == when
    runtime.close()


async def test_an_invalid_envelope_creates_no_event(tmp_path):
    """AT7: refusal is silent as far as the Event store is concerned."""
    runtime = Runtime(tmp_path / "bad.db")
    envelope = manual_envelope()
    envelope.source_event_key = ""

    result = await ingress(runtime).ingest(envelope)

    assert result.status is IngressStatus.REJECTED
    assert "empty source_event_key" in result.reasons
    assert result.event is None
    assert runtime.event_store.all() == []
    # Unkeyable, so there is nothing to file the rejection under.
    assert runtime.get_ingress_receipts() == []
    runtime.close()


async def test_a_keyable_rejection_is_still_recorded(tmp_path):
    """A refusal we *can* key is worth auditing: we were told, and we said no."""
    runtime = Runtime(tmp_path / "bad.db")
    envelope = manual_envelope(source_event_key="bad-001")
    envelope.event_type = ""

    result = await ingress(runtime).ingest(envelope)

    assert result.status is IngressStatus.REJECTED
    assert runtime.event_store.all() == []
    receipt = runtime.get_ingress_receipts()[0]
    assert receipt.status is IngressStatus.REJECTED
    assert receipt.reasons == ["empty event_type"]
    assert receipt.event_id is None
    runtime.close()


async def test_ingest_can_defer_running_the_system(tmp_path):
    """``deliver=False`` stores the event without activating anything."""
    from nexus_seed.processes.semantic import bootstrap_semantic

    runtime = Runtime(tmp_path / "defer.db")
    bootstrap_semantic(runtime)

    envelope = manual_envelope(
        event_type="process_parameter_changed",
        payload={"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )
    result = await ingress(runtime).ingest(envelope, deliver=False)

    assert result.accepted
    assert runtime.process_store.all_instances() == []
    assert runtime.state_store.get("D1_CD", "target") is None

    # Delivering later runs the very same event through the ordinary router.
    await runtime.deliver_event(result.event)
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()


async def test_a_batch_ingests_in_order(tmp_path):
    runtime = Runtime(tmp_path / "batch.db")
    envelopes = [manual_envelope(source_event_key=f"k-{n}") for n in range(3)]

    results = await ingress(runtime).ingest_batch(envelopes)

    assert [r.status for r in results] == [IngressStatus.ACCEPTED] * 3
    assert len(runtime.event_store.all()) == 3
    assert [r.source_event_key for r in runtime.get_ingress_receipts()] == [
        "k-0",
        "k-1",
        "k-2",
    ]
    runtime.close()
