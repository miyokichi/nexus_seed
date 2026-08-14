"""AT17 (spec §69): a resident observer ticking forever changes nothing.

An observer that runs for weeks re-reads the same tree thousands of times.  If
any layer counted re-observation as a change, the world model would drift on
its own.  Five mechanisms have to agree for that not to happen:

    Phase 2A  Event.id             a re-submitted event is a no-op
    Phase 2C  work_key             a state version yields work once
    Phase 3C  idempotency_key      an approved action acts once
    Phase 3D  source_event_key     a redelivered occurrence enters once
    Phase 3E  content_hash         unchanged bytes are not a new version
"""

from __future__ import annotations

from datetime import datetime, timezone

from resource_helpers import (
    FACT_DOCUMENT,
    full_stack,
    instances_named,
    watched_tree,
    write_file,
)

from nexus_seed.core.event import Event
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


def start() -> Event:
    return Event(
        "start_watch_files", "operator", {"adapter_id": "local_file", "poll_interval": 30}
    )


async def observing(tmp_path, out=None):
    root = watched_tree(tmp_path)
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "dup.db", clock=clock)
    adapter, backend = full_stack(runtime, root, out, observer=True)
    return runtime, clock, root, backend


async def test_many_quiet_cycles_produce_one_of_everything(tmp_path):
    """AT17: ten ticks over an unchanged file."""
    out = tmp_path / "out"
    runtime, clock, root, backend = await observing(tmp_path, out)
    write_file(root, "report.txt", FACT_DOCUMENT)

    await runtime.submit_event(start())
    for _ in range(9):
        clock.advance(30)
        await runtime.tick()

    resource = runtime.get_resource_by_uri("file:///report.txt")
    versions = runtime.get_resource_versions(resource.id)

    assert len(runtime.get_ingress_receipts()) == 1
    assert len(runtime.event_store.by_type("file_created")) == 1
    assert len(versions) == 1
    assert len(runtime.get_representations(versions[0].id)) == 1
    assert len(runtime.get_state_history("D1_CD", "analysis_result")) == 1
    assert len(runtime.get_work_requirements()) == 1
    assert len(runtime.get_action_proposals()) == 1
    assert len(list(out.glob("*.txt"))) == 1
    assert len(backend.calls) == 1

    # Ten cycles ran, and only the first one had anything to report.
    state = runtime.continuation_store.for_instance(
        instances_named(runtime, "watch_files")[0].id
    ).saved_process_state
    assert state["poll_count"] == 10
    assert state["ingested_total"] == 1
    runtime.close()


async def test_only_real_changes_advance_the_world(tmp_path):
    """Quiet ticks between two genuine edits stay quiet."""
    runtime, clock, root, _ = await observing(tmp_path)
    write_file(root, "spec.txt", "target=48")

    await runtime.submit_event(start())
    for _ in range(3):
        clock.advance(30)
        await runtime.tick()

    write_file(root, "spec.txt", "target=45")
    clock.advance(30)
    await runtime.tick()

    for _ in range(3):
        clock.advance(30)
        await runtime.tick()

    resource = runtime.get_resource_by_uri("file:///spec.txt")
    assert [v.version for v in runtime.get_resource_versions(resource.id)] == [1, 2]
    assert runtime.state_store.get("target", "value") is None  # nothing invented
    assert len(runtime.event_store.by_type("file_modified")) == 1
    runtime.close()


async def test_reverting_a_file_reuses_the_earlier_version(tmp_path):
    """A → B → A: the adapter sees three events, the catalogue holds two versions."""
    runtime, clock, root, _ = await observing(tmp_path)
    write_file(root, "spec.txt", "A")

    await runtime.submit_event(start())
    for content in ("B", "A"):
        write_file(root, "spec.txt", content)
        clock.advance(30)
        await runtime.tick()

    resource = runtime.get_resource_by_uri("file:///spec.txt")
    assert [v.version for v in runtime.get_resource_versions(resource.id)] == [1, 2]
    assert len(runtime.event_store.by_type("file_modified")) == 2
    runtime.close()


async def test_the_observer_does_not_accumulate_instances(tmp_path):
    """One resident process, however long it runs."""
    runtime, clock, root, _ = await observing(tmp_path)

    await runtime.submit_event(start())
    for _ in range(5):
        clock.advance(30)
        await runtime.tick()

    assert len(instances_named(runtime, "watch_files")) == 1
    # And exactly one continuation is outstanding for it.
    assert len(runtime.continuation_store.all()) == 1
    runtime.close()
