"""AT15, AT17, AT18 (spec §67, §69, §70): a long-lived observer, made of Process.

Phase 3D left one question open: who calls ``poll()``?  The answer is not a
daemon, a thread or a supervisor — it is a Process that suspends on a timer and
resumes (Invariant 41).  Between ticks it is a row in SQLite and a Continuation,
which is why it costs nothing to restart and everything it did is in the
ordinary process history.
"""

from __future__ import annotations

from datetime import datetime, timezone

from resource_helpers import instances_named, resource_runtime, watched_tree, write_file

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


def observer_runtime(tmp_path, db="watch.db"):
    """A runtime with the resource pipeline and the observer registered."""
    root = watched_tree(tmp_path)
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / db, clock=clock)
    resource_runtime(runtime, root, observer=True)
    return runtime, clock, root


def start(interval: float = 30.0, **extra) -> Event:
    payload = {"adapter_id": "local_file", "poll_interval": interval}
    payload.update(extra)
    return Event("start_watch_files", "operator", payload)


def watcher(runtime):
    return instances_named(runtime, "watch_files")[0]


async def test_the_observer_polls_suspends_and_resumes(tmp_path):
    """AT15: at least three cycles, driven only by the clock."""
    runtime, clock, root = observer_runtime(tmp_path)

    await runtime.submit_event(start())
    instance = watcher(runtime)
    assert instance.status is ProcessStatus.SUSPENDED

    continuation = runtime.continuation_store.for_instance(instance.id)
    assert continuation.resume_point == "poll"
    assert continuation.saved_process_state["poll_count"] == 1

    for expected in (2, 3):
        clock.advance(30)
        await runtime.tick()
        current = runtime.process_store.get_instance(instance.id)
        assert current.status is ProcessStatus.SUSPENDED
        state = runtime.continuation_store.for_instance(instance.id).saved_process_state
        assert state["poll_count"] == expected

    # One process instance ran three cycles — not three instances.
    assert len(instances_named(runtime, "watch_files")) == 1
    runtime.close()


async def test_repeated_cycles_over_an_unchanged_tree_change_nothing(tmp_path):
    """AT17: re-observing is not re-happening, all the way through."""
    runtime, clock, root = observer_runtime(tmp_path)
    write_file(root, "report.txt", "stable")

    await runtime.submit_event(start())
    for _ in range(3):
        clock.advance(30)
        await runtime.tick()

    assert len(runtime.get_ingress_receipts()) == 1
    assert len(runtime.event_store.by_type("file_created")) == 1
    resource = runtime.get_resource_by_uri("file:///report.txt")
    assert len(runtime.get_resource_versions(resource.id)) == 1
    version = runtime.get_current_resource_version(resource.id)
    assert len(runtime.get_representations(version.id)) == 1
    runtime.close()


async def test_a_change_between_cycles_is_picked_up(tmp_path):
    """AT18: cycle 1 sees v1, the file changes, cycle 2 sees v2."""
    runtime, clock, root = observer_runtime(tmp_path)
    write_file(root, "report.txt", "v1")

    await runtime.submit_event(start())
    resource = runtime.get_resource_by_uri("file:///report.txt")
    assert len(runtime.get_resource_versions(resource.id)) == 1

    write_file(root, "report.txt", "v2")
    clock.advance(30)
    await runtime.tick()

    versions = runtime.get_resource_versions(resource.id)
    assert [v.version for v in versions] == [1, 2]
    assert runtime.find_representation(versions[1].id, "text").content == "v2"
    assert [e.type for e in runtime.event_store.all() if e.is_external] == [
        "file_created",
        "file_modified",
    ]
    runtime.close()


async def test_ingested_events_are_routed_not_re_appended(tmp_path):
    """The observer's events are already durable; routing must not duplicate them."""
    runtime, clock, root = observer_runtime(tmp_path)
    write_file(root, "report.txt", "v1")

    await runtime.submit_event(start())

    created = runtime.event_store.by_type("file_created")
    assert len(created) == 1
    # One event, one receipt, one downstream indexer.
    assert runtime.get_ingress_receipts()[0].event_id == created[0].id
    assert len(instances_named(runtime, "resource_indexer")) == 1
    runtime.close()


async def test_the_observer_stops_on_request(tmp_path):
    """Spec §48: a resident process is controllable by an ordinary event."""
    runtime, clock, root = observer_runtime(tmp_path)

    await runtime.submit_event(start())
    instance = watcher(runtime)
    assert instance.status is ProcessStatus.SUSPENDED

    await runtime.submit_event(
        Event("stop_watch_files", "operator", {"adapter_id": "local_file"})
    )

    final = runtime.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.COMPLETED
    assert final.local_state["output"]["stopped"] is True
    assert runtime.continuation_store.for_instance(instance.id) is None
    runtime.close()


async def test_max_cycles_ends_the_loop(tmp_path):
    runtime, clock, root = observer_runtime(tmp_path)

    await runtime.submit_event(start(max_cycles=2))
    instance = watcher(runtime)
    assert instance.status is ProcessStatus.SUSPENDED

    clock.advance(30)
    await runtime.tick()

    final = runtime.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.COMPLETED
    assert final.local_state["output"]["poll_count"] == 2
    runtime.close()


async def test_a_failing_poll_is_reported_and_the_loop_continues(tmp_path):
    """Spec §45–§46: no infinite retry; the next tick is the retry."""
    runtime, clock, root = observer_runtime(tmp_path)
    adapter = [a for a in runtime.adapters][0]

    async def broken(*args, **kwargs):
        raise OSError("the disk went away")

    original = adapter.poll_and_ingest
    adapter.poll_and_ingest = broken

    await runtime.submit_event(start())
    instance = watcher(runtime)

    failures = runtime.event_store.by_type("observer_poll_failed")
    assert len(failures) == 1
    assert "disk went away" in failures[0].payload["error"]
    # Still alive and scheduled, not failed.
    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.SUSPENDED

    adapter.poll_and_ingest = original
    write_file(root, "report.txt", "recovered")
    clock.advance(30)
    await runtime.tick()

    assert runtime.get_resource_by_uri("file:///report.txt") is not None
    runtime.close()


async def test_an_unknown_adapter_fails_loudly(tmp_path):
    runtime, clock, root = observer_runtime(tmp_path)
    await runtime.submit_event(start(adapter_id="not_registered"))

    instance = watcher(runtime)
    assert instance.status is ProcessStatus.FAILED
    assert "no adapter registered" in instance.last_error
    runtime.close()
