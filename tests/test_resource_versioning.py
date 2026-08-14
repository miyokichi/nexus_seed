"""AT4 + AT5 (spec §56, §57): version history, current pointer, restart."""

from __future__ import annotations

from resource_helpers import only_resource, resource_runtime, watched_tree, write_file

from nexus_seed.runtime.runtime import Runtime


async def build_history(tmp_path, contents, *, db="v.db"):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / db)
    adapter = resource_runtime(runtime, root)
    for content in contents:
        write_file(root, "spec.txt", content)
        await adapter.poll_and_ingest()
    return runtime, adapter, root


async def test_history_keeps_every_version_and_current_is_the_last(tmp_path):
    """AT4."""
    runtime, _, _ = await build_history(tmp_path, ["v1", "v2", "v3"])
    resource = only_resource(runtime)

    versions = runtime.get_resource_versions(resource.id)
    assert [v.version for v in versions] == [1, 2, 3]
    assert len({v.content_hash for v in versions}) == 3
    assert runtime.get_current_resource_version(resource.id).version == 3

    # Any historical version is retrievable by number.
    assert runtime.get_resource_version(resource.id, 2).id == versions[1].id
    assert runtime.get_resource_version(resource.id, 9) is None
    runtime.close()


async def test_every_version_keeps_its_own_rendering(tmp_path):
    runtime, _, _ = await build_history(tmp_path, ["v1", "v2", "v3"])
    resource = only_resource(runtime)

    contents = [
        runtime.find_representation(v.id, "text").content
        for v in runtime.get_resource_versions(resource.id)
    ]
    assert contents == ["v1", "v2", "v3"]
    runtime.close()


async def test_each_version_records_the_event_that_produced_it(tmp_path):
    runtime, _, _ = await build_history(tmp_path, ["v1", "v2"])
    resource = only_resource(runtime)
    v1, v2 = runtime.get_resource_versions(resource.id)

    assert v1.source_event_id != v2.source_event_id
    assert runtime.event_store.get(v1.source_event_id).type == "file_created"
    assert runtime.event_store.get(v2.source_event_id).type == "file_modified"
    runtime.close()


async def test_history_survives_a_restart(tmp_path):
    """AT5."""
    db_path = tmp_path / "restart.db"
    runtime, _, root = await build_history(tmp_path, ["v1", "v2", "v3"], db="restart.db")
    resource_id = only_resource(runtime).id
    hashes = [v.content_hash for v in runtime.get_resource_versions(resource_id)]
    runtime.close()

    runtime2 = Runtime(db_path)
    resource_runtime(runtime2, root)

    resource = runtime2.get_resource(resource_id)
    assert resource.uri == "file:///spec.txt"
    assert [v.content_hash for v in runtime2.get_resource_versions(resource_id)] == hashes
    assert runtime2.get_current_resource_version(resource_id).version == 3

    # And the renderings came back too.
    v3 = runtime2.get_current_resource_version(resource_id)
    assert runtime2.find_representation(v3.id, "text").content == "v3"
    runtime2.close()


async def test_a_new_version_after_a_restart_continues_the_numbering(tmp_path):
    db_path = tmp_path / "restart.db"
    runtime, _, root = await build_history(tmp_path, ["v1", "v2"], db="restart.db")
    resource_id = only_resource(runtime).id
    runtime.close()

    runtime2 = Runtime(db_path)
    adapter2 = resource_runtime(runtime2, root)
    write_file(root, "spec.txt", "v3")
    await adapter2.poll_and_ingest()

    assert [v.version for v in runtime2.get_resource_versions(resource_id)] == [1, 2, 3]
    runtime2.close()


async def test_a_deleted_file_leaves_its_history_intact(tmp_path):
    """The document is gone from the world; what it said is still on record."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "del.db")
    adapter = resource_runtime(runtime, root)

    write_file(root, "spec.txt", "v1")
    await adapter.poll_and_ingest()
    (root / "spec.txt").unlink()
    await adapter.poll_and_ingest()

    resource = only_resource(runtime)
    versions = runtime.get_resource_versions(resource.id)
    assert [v.version for v in versions] == [1]
    assert runtime.find_representation(versions[0].id, "text").content == "v1"
    assert runtime.event_store.by_type("file_deleted") != []
    runtime.close()
