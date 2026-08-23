"""Manual observations preserve external identity at the Ingress boundary."""

from __future__ import annotations

from ingress_helpers import ingress

from nexus_seed.adapters.manual import ManualAdapter
from nexus_seed.ingress.models import IngressStatus
from nexus_seed.runtime.runtime import Runtime


def test_the_adapter_only_describes_what_it_was_given():
    adapter = ManualAdapter()
    envelope = adapter.envelope(
        event_type="human_message",
        source_event_key="demo-001",
        payload={"text": "hello"},
        metadata={"who": "alex"},
    )
    assert envelope.adapter_id == "manual"
    assert envelope.source_type == "manual"
    assert envelope.source_event_key == "demo-001"
    assert envelope.event_type == "human_message"
    assert envelope.payload == {"text": "hello"}
    assert envelope.metadata == {"who": "alex"}


def test_an_adapter_cannot_reach_state_or_processes():
    """Invariants 29/34: adapters describe, they do not act or interpret."""
    adapter = ManualAdapter()
    for forbidden in (
        "state_store",
        "runtime",
        "process_store",
        "observe",
        "propose_delta",
        "spawn",
    ):
        assert not hasattr(adapter, forbidden)


def test_a_custom_adapter_id_partitions_the_key_space():
    a = ManualAdapter("import_job").envelope(
        event_type="row_loaded", source_event_key="1"
    )
    assert a.adapter_id == "import_job"


async def test_manual_ingest_then_redelivery(tmp_path):
    runtime = Runtime(tmp_path / "manual.db")
    service = ingress(runtime)
    adapter = ManualAdapter()

    def envelope():
        return adapter.envelope(
            event_type="human_message",
            source_event_key="demo-001",
            payload={"text": "D1のCD解析が完了しました"},
        )

    first = await service.ingest(envelope())
    second = await service.ingest(envelope())

    assert first.status is IngressStatus.ACCEPTED
    assert second.status is IngressStatus.DUPLICATE
    assert len(runtime.event_store.all()) == 1
    runtime.close()
