"""AT16 (spec §68): a resident observer survives being switched off.

A daemon has to be restarted and told where it got to.  A Process suspended on
a timer just... resumes: the timer, the continuation and the adapter's
checkpoint are all in SQLite, so the observer inherits Phase 2A restart safety
for free — including *not* being recreated as a second instance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from resource_helpers import instances_named, resource_runtime, watched_tree, write_file

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


def rebuild(db_path, root, at=None):
    """Build a fresh Runtime + observer over the same database and tree."""
    clock = ManualClock(at or datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(db_path, clock=clock)
    resource_runtime(runtime, root, observer=True)
    return runtime, clock


def start() -> Event:
    return Event(
        "start_watch_files", "operator", {"adapter_id": "local_file", "poll_interval": 30}
    )


async def test_the_observer_resumes_after_a_restart(tmp_path):
    db_path = tmp_path / "watch.db"
    root = watched_tree(tmp_path)
    write_file(root, "report.txt", "v1")

    runtime, clock = rebuild(db_path, root)
    await runtime.submit_event(start())
    instance_id = instances_named(runtime, "watch_files")[0].id
    assert runtime.get_resource_by_uri("file:///report.txt") is not None
    runtime.close()

    # --- everything in memory is discarded ---
    runtime2, clock2 = rebuild(db_path, root, at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    survivor = runtime2.process_store.get_instance(instance_id)
    assert survivor.status is ProcessStatus.SUSPENDED
    continuation = runtime2.continuation_store.for_instance(instance_id)
    assert continuation.resume_point == "poll"
    assert continuation.saved_process_state["poll_count"] == 1

    # A change made while nothing was watching is picked up on the next tick.
    write_file(root, "report.txt", "v2")
    clock2.advance(30)
    await runtime2.tick()

    assert runtime2.process_store.get_instance(instance_id).status is ProcessStatus.SUSPENDED
    # Still exactly one observer — the restart did not spawn a second.
    assert len(instances_named(runtime2, "watch_files")) == 1

    resource = runtime2.get_resource_by_uri("file:///report.txt")
    assert [v.version for v in runtime2.get_resource_versions(resource.id)] == [1, 2]
    runtime2.close()


async def test_an_unchanged_tree_produces_nothing_after_a_restart(tmp_path):
    """AT17 across a restart: the checkpoint and the dedup both survive."""
    db_path = tmp_path / "watch.db"
    root = watched_tree(tmp_path)
    write_file(root, "report.txt", "stable")

    runtime, clock = rebuild(db_path, root)
    await runtime.submit_event(start())
    receipts_before = len(runtime.get_ingress_receipts())
    runtime.close()

    runtime2, clock2 = rebuild(db_path, root)
    for _ in range(3):
        clock2.advance(30)
        await runtime2.tick()

    assert len(runtime2.get_ingress_receipts()) == receipts_before == 1
    resource = runtime2.get_resource_by_uri("file:///report.txt")
    assert len(runtime2.get_resource_versions(resource.id)) == 1
    runtime2.close()


async def test_the_cycle_count_carries_across_the_restart(tmp_path):
    """The observer's own state is in its Continuation, so it is not reset.

    The pending timer is an *absolute* fire time, so the restarted runtime does
    not re-fire it early and does not lose it: it fires once wall-clock time
    reaches the moment the previous cycle scheduled.
    """
    db_path = tmp_path / "watch.db"
    root = watched_tree(tmp_path)

    runtime, clock = rebuild(db_path, root)
    await runtime.submit_event(start())  # poll 1, next timer at T+30
    clock.advance(30)
    await runtime.tick()  # poll 2, next timer at T+60
    instance_id = instances_named(runtime, "watch_files")[0].id
    assert runtime.continuation_store.for_instance(instance_id).saved_process_state[
        "poll_count"
    ] == 2
    runtime.close()

    runtime2, clock2 = rebuild(db_path, root)

    # Not yet due: a restart must not make the next cycle fire early.
    clock2.advance(30)
    await runtime2.tick()
    assert runtime2.continuation_store.for_instance(instance_id).saved_process_state[
        "poll_count"
    ] == 2

    clock2.advance(30)
    await runtime2.tick()

    state = runtime2.continuation_store.for_instance(instance_id).saved_process_state
    assert state["poll_count"] == 3
    assert state["adapter_id"] == "local_file"
    runtime2.close()


async def test_a_crashed_observer_is_recovered_to_runnable(tmp_path):
    """Phase 2A crash recovery applies to a resident process like any other."""
    db_path = tmp_path / "watch.db"
    root = watched_tree(tmp_path)

    runtime, clock = rebuild(db_path, root)
    await runtime.submit_event(start())
    instance = instances_named(runtime, "watch_files")[0]

    # Simulate an interruption mid-activation.
    instance.status = ProcessStatus.RUNNING
    runtime.process_store.save_instance(instance)
    runtime.close()

    runtime2, clock2 = rebuild(db_path, root)
    recovered = runtime2.process_store.get_instance(instance.id)
    assert recovered.status is ProcessStatus.RUNNABLE

    await runtime2.run_pending()
    assert runtime2.process_store.get_instance(instance.id).status is ProcessStatus.SUSPENDED
    assert len(instances_named(runtime2, "watch_files")) == 1
    runtime2.close()
