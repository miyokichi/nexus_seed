"""AT1–AT3 (spec §53–§55): file events become catalogued Resources.

The adapter says bytes changed; the indexer decides whether that is a new
document, a new version of one, or nothing at all.  Keeping those two jobs in
different layers (spec §10) is why the adapter never touches these tables.
"""

from __future__ import annotations

from resource_helpers import instances_named, only_resource, resource_runtime, watched_tree

from nexus_seed.runtime.runtime import Runtime


async def test_a_new_file_becomes_a_resource_at_version_one(tmp_path):
    """AT1."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)

    (root / "report.txt").write_text("first", encoding="utf-8")
    await adapter.poll_and_ingest()

    resource = only_resource(runtime)
    assert resource.uri == "file:///report.txt"
    assert resource.resource_type == "text"
    assert resource.source_adapter_id == "local_file"

    versions = runtime.get_resource_versions(resource.id)
    assert [v.version for v in versions] == [1]
    assert versions[0].content_hash.startswith("sha256-")
    assert versions[0].size_bytes == 5
    assert versions[0].locator == "report.txt"

    current = runtime.get_current_resource_version(resource.id)
    assert current.id == versions[0].id
    assert resource.current_version_id == current.id
    runtime.close()


async def test_editing_a_file_appends_a_version_to_the_same_resource(tmp_path):
    """AT2: the document keeps its identity; its content gets a history."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)
    target = root / "report.txt"

    target.write_text("first", encoding="utf-8")
    await adapter.poll_and_ingest()
    target.write_text("second", encoding="utf-8")
    await adapter.poll_and_ingest()

    resource = only_resource(runtime)
    versions = runtime.get_resource_versions(resource.id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].content_hash != versions[1].content_hash
    assert runtime.get_current_resource_version(resource.id).version == 2
    runtime.close()


async def test_identical_content_creates_no_new_version(tmp_path):
    """AT3: re-observing the same bytes is not a change."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)
    target = root / "report.txt"

    target.write_text("stable", encoding="utf-8")
    await adapter.poll_and_ingest()
    for _ in range(3):
        await adapter.poll_and_ingest()

    resource = only_resource(runtime)
    assert len(runtime.get_resource_versions(resource.id)) == 1
    assert len(runtime.event_store.by_type("resource_version_created")) == 1
    runtime.close()


async def test_rewriting_the_same_bytes_is_not_a_new_version(tmp_path):
    """Even a genuine file_modified event with unchanged content is a no-op.

    The adapter's dedup is by (path, change, hash); the indexer's is by content
    against the *whole* history — so a revert to an earlier version is caught
    here even though the adapter reports it as a change.
    """
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)
    target = root / "report.txt"

    for content in ("A", "B", "A"):
        target.write_text(content, encoding="utf-8")
        await adapter.poll_and_ingest()

    resource = only_resource(runtime)
    # Three file events, but "A" is a version we already hold.
    assert len(runtime.event_store.by_type("file_modified")) == 2
    assert [v.version for v in runtime.get_resource_versions(resource.id)] == [1, 2]

    indexers = instances_named(runtime, "resource_indexer")
    assert len(indexers) == 3
    assert indexers[-1].local_state["output"]["indexed"] is False
    assert indexers[-1].local_state["output"]["reason"] == "content_hash unchanged"
    runtime.close()


async def test_separate_files_are_separate_resources(tmp_path):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)

    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    await adapter.poll_and_ingest()

    assert sorted(r.uri for r in runtime.get_resources()) == [
        "file:///a.txt",
        "file:///b.txt",
    ]
    runtime.close()


async def test_resource_type_comes_from_the_suffix(tmp_path):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)

    for name, content in (
        ("notes.txt", "x"),
        ("data.csv", "a,b\n1,2\n"),
        ("conf.json", "{}"),
        ("thing.bin", "?"),
    ):
        (root / name).write_text(content, encoding="utf-8")
    await adapter.poll_and_ingest()

    types = {r.uri: r.resource_type for r in runtime.get_resources()}
    assert types["file:///notes.txt"] == "text"
    assert types["file:///data.csv"] == "csv"
    assert types["file:///conf.json"] == "json"
    assert types["file:///thing.bin"] == "unknown"
    runtime.close()


async def test_a_version_traces_back_to_the_external_source(tmp_path):
    """Spec §24: which delivery told us about these bytes?"""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)

    (root / "report.txt").write_text("first", encoding="utf-8")
    await adapter.poll_and_ingest()

    resource = only_resource(runtime)
    version = runtime.get_current_resource_version(resource.id)
    assert version.source_event_id is not None
    assert version.ingress_receipt_id is not None

    trace = runtime.get_resource_trace(version.id)
    assert trace.resource.uri == "file:///report.txt"
    assert trace.source_event.type == "file_created"
    assert trace.external_identity[0] == "local_file"
    assert trace.external_identity[1].startswith("file:report.txt:file_created:")
    runtime.close()
