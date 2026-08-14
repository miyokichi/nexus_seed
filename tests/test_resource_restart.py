"""AT5 (spec §57): the catalogue is rebuildable from SQLite alone.

The same discard-and-rebuild discipline every phase is held to, applied to
documents: after a restart the Resource, its whole version history, its current
pointer and every rendering must come back — and a re-scan of the unchanged
tree must add nothing.
"""

from __future__ import annotations

from resource_helpers import resource_runtime, watched_tree, write_file

from nexus_seed.runtime.runtime import Runtime


async def test_resources_versions_and_renderings_all_come_back(tmp_path):
    db_path = tmp_path / "restart.db"
    root = watched_tree(tmp_path)

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    for content in ("v1", "v2", "v3"):
        write_file(root, "spec.txt", content)
        await adapter.poll_and_ingest()
    write_file(root, "notes.txt", "unrelated")
    await adapter.poll_and_ingest()

    before = {
        r.uri: [v.content_hash for v in runtime.get_resource_versions(r.id)]
        for r in runtime.get_resources()
    }
    runtime.close()

    # --- everything in memory is discarded ---
    runtime2 = Runtime(db_path)
    adapter2 = resource_runtime(runtime2, root)

    after = {
        r.uri: [v.content_hash for v in runtime2.get_resource_versions(r.id)]
        for r in runtime2.get_resources()
    }
    assert after == before
    assert sorted(after) == ["file:///notes.txt", "file:///spec.txt"]

    spec = runtime2.get_resource_by_uri("file:///spec.txt")
    assert runtime2.get_current_resource_version(spec.id).version == 3
    assert runtime2.find_representation(
        runtime2.get_current_resource_version(spec.id).id, "text"
    ).content == "v3"

    # Historical renderings survive too.
    v1 = runtime2.get_resource_version(spec.id, 1)
    assert runtime2.find_representation(v1.id, "text").content == "v1"

    # And a re-scan of the unchanged tree adds nothing.
    await adapter2.poll_and_ingest()
    assert len(runtime2.get_resource_versions(spec.id)) == 3
    runtime2.close()


async def test_provenance_survives_the_restart(tmp_path):
    """The chain out to the external source is stored, not reconstructed."""
    db_path = tmp_path / "restart.db"
    root = watched_tree(tmp_path)

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    write_file(root, "spec.txt", "v1")
    await adapter.poll_and_ingest()
    resource_id = runtime.get_resource_by_uri("file:///spec.txt").id
    version_id = runtime.get_current_resource_version(resource_id).id
    runtime.close()

    runtime2 = Runtime(db_path)
    resource_runtime(runtime2, root)

    trace = runtime2.get_resource_trace(version_id)
    assert trace.resource.uri == "file:///spec.txt"
    assert trace.source_event.type == "file_created"
    assert trace.ingress_receipt.adapter_id == "local_file"
    assert [r.representation_type for r in trace.representations] == ["text"]

    representation_trace = runtime2.get_representation_trace(trace.representations[0].id)
    assert representation_trace.extractor == ("plain_text", "1")
    assert representation_trace.extracted_by.definition_name == "extract_resource"
    runtime2.close()


async def test_a_change_made_while_stopped_is_indexed_once_on_return(tmp_path):
    db_path = tmp_path / "restart.db"
    root = watched_tree(tmp_path)

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    write_file(root, "spec.txt", "v1")
    await adapter.poll_and_ingest()
    resource_id = runtime.get_resource_by_uri("file:///spec.txt").id
    runtime.close()

    # The world moves on while nothing is watching.
    write_file(root, "spec.txt", "v2")

    runtime2 = Runtime(db_path)
    adapter2 = resource_runtime(runtime2, root)
    await adapter2.poll_and_ingest()
    await adapter2.poll_and_ingest()  # and again, for good measure

    assert [v.version for v in runtime2.get_resource_versions(resource_id)] == [1, 2]
    runtime2.close()


async def test_the_current_pointer_is_rebuildable_from_the_versions(tmp_path):
    """The pointer is a projection; losing it costs a lookup, not the truth."""
    db_path = tmp_path / "restart.db"
    root = watched_tree(tmp_path)

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    for content in ("v1", "v2"):
        write_file(root, "spec.txt", content)
        await adapter.poll_and_ingest()
    resource_id = runtime.get_resource_by_uri("file:///spec.txt").id
    runtime.close()

    runtime2 = Runtime(db_path)
    resource_runtime(runtime2, root)
    runtime2.db.execute(
        "UPDATE resources SET current_version_id = NULL WHERE id = ?", (str(resource_id),)
    )

    assert runtime2.get_current_resource_version(resource_id).version == 2
    runtime2.close()
