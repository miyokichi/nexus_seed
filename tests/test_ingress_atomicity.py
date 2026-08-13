"""AT4 (spec §54): the receipt, the Event and the checkpoint land together.

The dangerous partial states are all in one direction:

* a receipt without an Event  -> the occurrence is marked handled but nothing
  ever happens, and the redelivery that would have fixed it is deduplicated
  away — a *silently lost* external event;
* a checkpoint ahead of the receipts -> the adapter skips past occurrences it
  never actually ingested.

Both are prevented by committing the three writes in one transaction, so an
exception anywhere in the middle leaves nothing behind and a retry converges.
"""

from __future__ import annotations

import pytest
from ingress_helpers import ingress, manual_envelope

from nexus_seed.ingress.models import AdapterCheckpoint
from nexus_seed.runtime.runtime import Runtime


class Boom(Exception):
    """Injected failure."""


async def test_a_failure_after_the_receipt_leaves_nothing_behind(tmp_path):
    runtime = Runtime(tmp_path / "atomic.db")
    service = ingress(runtime)

    original_append = runtime.event_store.append

    def explode(event):
        raise Boom("disk died between the receipt and the event")

    runtime.event_store.append = explode
    with pytest.raises(Boom):
        await service.ingest(manual_envelope(source_event_key="demo-001"))

    # Neither half survived.
    assert runtime.get_ingress_receipts() == []
    assert runtime.event_store.all() == []

    # And the very same delivery now succeeds — the retry converges.
    runtime.event_store.append = original_append
    result = await service.ingest(manual_envelope(source_event_key="demo-001"))
    assert result.accepted
    assert len(runtime.event_store.all()) == 1
    assert len(runtime.get_ingress_receipts()) == 1
    runtime.close()


async def test_a_failure_does_not_advance_the_checkpoint(tmp_path):
    """An adapter must never believe it observed past something it did not ingest."""
    runtime = Runtime(tmp_path / "atomic.db")
    service = ingress(runtime)
    service.checkpoints.save(AdapterCheckpoint("local_file", "a.txt", "sha256-old"))

    def explode(event):
        raise Boom("crash before commit")

    runtime.event_store.append = explode
    with pytest.raises(Boom):
        await service.ingest(
            manual_envelope(source_event_key="v2"),
            checkpoint=AdapterCheckpoint("local_file", "a.txt", "sha256-new"),
        )

    assert runtime.get_adapter_checkpoint("local_file", "a.txt").cursor == "sha256-old"
    assert runtime.get_ingress_receipts() == []
    runtime.close()


async def test_a_successful_ingest_commits_all_three(tmp_path):
    runtime = Runtime(tmp_path / "atomic.db")
    result = await ingress(runtime).ingest(
        manual_envelope(source_event_key="v2"),
        checkpoint=AdapterCheckpoint("local_file", "a.txt", "sha256-new"),
    )

    assert result.accepted
    assert runtime.get_ingress_receipts()[0].event_id == result.event.id
    assert runtime.event_store.get(result.event.id) is not None
    assert runtime.get_adapter_checkpoint("local_file", "a.txt").cursor == "sha256-new"
    runtime.close()


async def test_a_crash_while_processing_does_not_lose_the_event(tmp_path):
    """Delivery happens after the commit, so a handler blowing up is recoverable.

    The event is already durable; the process that failed on it is FAILED and
    visible, rather than the occurrence vanishing.
    """
    from nexus_seed.core.process import ProcessDefinition

    runtime = Runtime(tmp_path / "handler.db")

    async def always_fails(ctx):
        raise Boom("handler exploded")

    runtime.register_process(
        ProcessDefinition(
            name="explodes",
            version="1",
            handler="explodes",
            trigger_event_types=("human_message",),
        ),
        always_fails,
    )

    result = await ingress(runtime).ingest(manual_envelope(source_event_key="demo-001"))

    assert result.accepted
    assert runtime.event_store.get(result.event.id) is not None
    assert runtime.get_ingress_receipts()[0].event_id == result.event.id
    instance = runtime.process_store.all_instances()[0]
    assert instance.status.value == "FAILED"
    runtime.close()
