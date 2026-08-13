"""AT2 + AT3 (spec §52, §53): one external occurrence, one Event.

The whole point of holding a separate ``source_event_key``: the outside world
decides what counts as "the same thing happening", and NEXUS SEED honours that
decision in both directions — a redelivery is not new, and two genuinely
different occurrences are not the same just because they look alike.
"""

from __future__ import annotations

from ingress_helpers import ingress, manual_envelope

from nexus_seed.ingress.models import IngressStatus
from nexus_seed.runtime.runtime import Runtime


async def test_the_same_source_key_yields_one_event(tmp_path):
    """AT2."""
    runtime = Runtime(tmp_path / "dup.db")
    service = ingress(runtime)

    first = await service.ingest(manual_envelope(source_event_key="demo-001"))
    second = await service.ingest(manual_envelope(source_event_key="demo-001"))

    assert first.status is IngressStatus.ACCEPTED
    assert second.status is IngressStatus.DUPLICATE

    assert len(runtime.event_store.all()) == 1
    assert len(runtime.get_ingress_receipts()) == 1
    # The duplicate points back at the original receipt and event.
    assert second.receipt.id == first.receipt.id
    assert second.event.id == first.event.id
    runtime.close()


async def test_a_redelivery_is_still_a_duplicate_after_a_restart(tmp_path):
    """Dedup lives in SQLite, so it outlives the process that did the first one."""
    db_path = tmp_path / "dup.db"
    runtime = Runtime(db_path)
    first = await ingress(runtime).ingest(manual_envelope(source_event_key="demo-001"))
    runtime.close()

    runtime2 = Runtime(db_path)
    second = await ingress(runtime2).ingest(manual_envelope(source_event_key="demo-001"))

    assert second.status is IngressStatus.DUPLICATE
    assert second.event.id == first.event.id
    assert len(runtime2.event_store.all()) == 1
    runtime2.close()


async def test_a_different_source_key_is_a_different_occurrence(tmp_path):
    """AT3: identical payloads, distinct external identities -> two events."""
    runtime = Runtime(tmp_path / "distinct.db")
    service = ingress(runtime)
    payload = {"text": "the same words"}

    a = await service.ingest(manual_envelope(source_event_key="msg-1", payload=payload))
    b = await service.ingest(manual_envelope(source_event_key="msg-2", payload=payload))

    assert a.accepted and b.accepted
    assert a.event.id != b.event.id
    assert len(runtime.event_store.all()) == 2
    runtime.close()


async def test_the_same_key_from_two_adapters_is_two_occurrences(tmp_path):
    """Identity is the *pair*: a delivery id only means something per source."""
    runtime = Runtime(tmp_path / "adapters.db")
    service = ingress(runtime)

    a = await service.ingest(manual_envelope(adapter_id="manual", source_event_key="1"))
    b = await service.ingest(manual_envelope(adapter_id="webhook", source_event_key="1"))

    assert a.accepted and b.accepted
    assert len(runtime.event_store.all()) == 2
    runtime.close()


async def test_a_duplicate_does_not_re_run_the_pipeline(tmp_path):
    """No second Event means no second interpretation and no second process."""
    from nexus_seed.processes.semantic import bootstrap_semantic

    runtime = Runtime(tmp_path / "pipeline.db")
    bootstrap_semantic(runtime)
    service = ingress(runtime)

    envelope_kw = dict(
        event_type="process_parameter_changed",
        source_event_key="change-001",
        payload={"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )
    await service.ingest(manual_envelope(**envelope_kw))

    processes_after_first = len(runtime.process_store.all_instances())
    deltas_after_first = len(runtime.state_delta_store.all())
    history_after_first = len(runtime.get_state_history("D1_CD", "target"))

    await service.ingest(manual_envelope(**envelope_kw))

    assert len(runtime.process_store.all_instances()) == processes_after_first
    assert len(runtime.state_delta_store.all()) == deltas_after_first
    assert len(runtime.get_state_history("D1_CD", "target")) == history_after_first == 1
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()


async def test_a_duplicate_still_advances_the_checkpoint(tmp_path):
    """We *have* now looked past this point, even though nothing was new."""
    from nexus_seed.ingress.models import AdapterCheckpoint

    runtime = Runtime(tmp_path / "cp.db")
    service = ingress(runtime)
    envelope = manual_envelope(source_event_key="demo-001")

    await service.ingest(envelope, checkpoint=AdapterCheckpoint("manual", "s", "1"))
    result = await service.ingest(
        envelope, checkpoint=AdapterCheckpoint("manual", "s", "2")
    )

    assert result.duplicate
    assert runtime.get_adapter_checkpoint("manual", "s").cursor == "2"
    runtime.close()
