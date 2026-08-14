"""AT10 + AT11 (spec §61, §62): the Phase 3E gap, closed and pinned.

The bug this phase exists for.  In Phase 3E an observer could ingest an event —
committing it with its receipt, consuming the source key — and then fail before
the event was routed.  Nothing would ever re-offer it: the file scan would see
"already ingested", and the event would sit in the database forever, real and
unread.

The fix is not "try harder to route"; it is to make the *obligation* durable at
the moment the event is (Invariant 47).  These tests recreate the exact failure
and assert the loop still completes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from resource_helpers import (
    FACT_DOCUMENT,
    instances_named,
    resource_runtime,
    watched_tree,
    write_file,
)

from nexus_seed.adapters.file_watch import LocalFileAdapter
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


async def test_an_ingested_but_unrouted_event_is_still_delivered(tmp_path):
    """AT10: the regression, at its narrowest."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "gap.db")
    adapter = resource_runtime(runtime, root)

    write_file(root, "report.txt", "hello")
    # Ingest without delivering: exactly the state a crashed observer leaves.
    await adapter.poll_and_ingest(deliver=False)

    event = runtime.event_store.by_type("file_created")[0]
    assert runtime.get_event_delivery(event.id).status.value == "PENDING"
    assert runtime.get_resources() == []

    # The source will not offer it again — the key is spent.
    assert await adapter.poll() == []

    # The obligation is what carries it forward.
    await runtime.run_pending()

    assert runtime.get_event_delivery(event.id).status.value == "DELIVERED"
    assert runtime.get_resource_by_uri("file:///report.txt") is not None
    assert len(instances_named(runtime, "resource_indexer")) == 1
    runtime.close()


async def test_the_gap_survives_a_runtime_restart(tmp_path):
    """The event was durable, unread, and the process that owed it is gone."""
    db_path = tmp_path / "gap.db"
    root = watched_tree(tmp_path)

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    write_file(root, "report.txt", "hello")
    await adapter.poll_and_ingest(deliver=False)
    event_id = runtime.event_store.by_type("file_created")[0].id
    runtime.close()

    runtime2 = Runtime(db_path)
    adapter2 = resource_runtime(runtime2, root)
    assert runtime2.get_pending_event_delivery_count() == 1
    assert await adapter2.poll() == []

    await runtime2.run_pending()

    assert runtime2.get_event_delivery(event_id).status.value == "DELIVERED"
    resource = runtime2.get_resource_by_uri("file:///report.txt")
    assert resource is not None
    version = runtime2.get_current_resource_version(resource.id)
    assert runtime2.find_representation(version.id, "text").content == "hello"
    runtime2.close()


async def test_the_observer_no_longer_owns_routing(tmp_path):
    """watch_files ingests; it does not carry events onward any more."""
    root = watched_tree(tmp_path)
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "obs.db", clock=clock)
    resource_runtime(runtime, root, observer=True)

    from nexus_seed.core.event import Event

    write_file(root, "report.txt", "hello")
    await runtime.submit_event(
        Event("start_watch_files", "operator", {"adapter_id": "local_file", "poll_interval": 30})
    )

    # The observer's ProcessResult carries no routing responsibility at all.
    from nexus_seed.core.process import ProcessResult

    assert not hasattr(ProcessResult, "events_to_route")
    assert not hasattr(ProcessResult(status=None), "events_to_route")

    assert runtime.get_resource_by_uri("file:///report.txt") is not None
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_redelivery_still_converges_after_the_gap_is_filled(tmp_path):
    """AT11: closing the gap must not weaken ingress dedup."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "gap.db")
    adapter = resource_runtime(runtime, root)

    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest(deliver=False)
    await runtime.run_pending()

    for _ in range(3):
        await adapter.poll_and_ingest()

    resource = runtime.get_resource_by_uri("file:///report.txt")
    versions = runtime.get_resource_versions(resource.id)

    assert len(runtime.get_ingress_receipts()) == 1
    assert len(runtime.event_store.by_type("file_created")) == 1
    assert len(runtime.get_event_deliveries()) == len(runtime.event_store.all())
    assert len(versions) == 1
    assert len(runtime.get_representations(versions[0].id)) == 1
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_every_persisted_event_has_exactly_one_obligation(tmp_path):
    """The structural claim behind the fix (Invariant 43)."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "gap.db")
    adapter = resource_runtime(runtime, root)

    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()

    events = runtime.event_store.all()
    deliveries = runtime.get_event_deliveries()

    assert len(events) > 1
    assert len(deliveries) == len(events)
    assert {d.event_id for d in deliveries} == {e.id for e in events}
    assert all(d.status.value == "DELIVERED" for d in deliveries)
    runtime.close()
