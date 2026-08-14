"""AT16 (spec §67, §37): resource events are covered, without duplicating work.

``resource_version_created`` and ``representation_created`` carry the artifact
pipeline.  A crash before either is routed must not strand a document
half-catalogued — and the recovery must not produce a second Representation,
because the extraction dedup is a *different* mechanism that has to keep
holding on its own.
"""

from __future__ import annotations

from resource_helpers import (
    instances_named,
    only_resource,
    resource_runtime,
    watched_tree,
    write_file,
)

from nexus_seed.runtime.runtime import Runtime


async def test_every_resource_event_gets_an_obligation(tmp_path):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)

    write_file(root, "report.txt", "hello")
    await adapter.poll_and_ingest()

    for event_type in ("file_created", "resource_version_created", "representation_created"):
        event = runtime.event_store.by_type(event_type)[0]
        assert runtime.get_event_delivery(event.id).status.value == "DELIVERED"
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_a_crash_before_extraction_still_produces_one_representation(tmp_path):
    """AT16: indexed, then died; the restart extracts exactly once."""
    db_path = tmp_path / "r.db"
    root = watched_tree(tmp_path)

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    write_file(root, "report.txt", "hello")

    # Drive only as far as the indexer, then stop.
    await adapter.poll_and_ingest(deliver=False)
    runtime.dispatch_pending_events()
    instance = runtime.scheduler.next_runnable()
    await runtime.executor.execute(instance)

    resource = only_resource(runtime)
    version = runtime.get_current_resource_version(resource.id)
    created = runtime.event_store.by_type("resource_version_created")[0]
    assert runtime.get_event_delivery(created.id).status.value == "PENDING"
    assert runtime.get_representations(version.id) == []
    runtime.close()

    # --- restart ---
    runtime2 = Runtime(db_path)
    adapter2 = resource_runtime(runtime2, root)
    assert runtime2.get_pending_event_delivery_count() == 1

    await runtime2.run_pending()

    assert runtime2.get_event_delivery(created.id).status.value == "DELIVERED"
    representations = runtime2.get_representations(version.id)
    assert len(representations) == 1
    assert representations[0].content == "hello"
    assert len(instances_named(runtime2, "extract_resource")) == 1
    runtime2.close()


async def test_a_crash_before_interpretation_still_reaches_world_state(tmp_path):
    """The representation existed; the fact it carried must still land."""
    from nexus_seed.processes.semantic import bootstrap_semantic

    db_path = tmp_path / "r.db"
    root = watched_tree(tmp_path)

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    bootstrap_semantic(runtime)
    write_file(root, "facts.txt", "D1_CD.target=45\n")

    await adapter.poll_and_ingest(deliver=False)
    for _ in range(2):  # index, then extract
        runtime.dispatch_pending_events()
        instance = runtime.scheduler.next_runnable()
        if instance is None:
            break
        await runtime.executor.execute(instance)

    created = runtime.event_store.by_type("representation_created")[0]
    assert runtime.get_event_delivery(created.id).status.value == "PENDING"
    assert runtime.state_store.get("D1_CD", "target") is None
    runtime.close()

    runtime2 = Runtime(db_path)
    resource_runtime(runtime2, root)
    bootstrap_semantic(runtime2)
    await runtime2.run_pending()

    assert runtime2.state_store.get("D1_CD", "target") == 45
    assert len(runtime2.state_delta_store.all()) == 1
    assert len(runtime2.get_state_history("D1_CD", "target")) == 1
    runtime2.close()


async def test_a_re_delivered_resource_event_adds_no_second_representation(tmp_path):
    """Delivery retry and representation dedup are independent, and both hold."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)

    write_file(root, "report.txt", "hello")
    await adapter.poll_and_ingest()
    resource = only_resource(runtime)
    version = runtime.get_current_resource_version(resource.id)

    created = runtime.event_store.by_type("resource_version_created")[0]
    runtime.db.execute(
        "UPDATE event_deliveries SET status = 'PENDING' WHERE event_id = ?",
        (str(created.id),),
    )
    await runtime.run_pending()

    assert len(runtime.get_representations(version.id)) == 1
    assert len(runtime.get_resource_versions(resource.id)) == 1
    runtime.close()
