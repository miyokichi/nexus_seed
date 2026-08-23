"""AT9 + AT10 + AT11 (spec §59, §60, §61): watching a directory.

The adapter reports *that* files changed and refuses to say what the change
means (Invariant 34).  Its identity scheme is content-based, so "nothing
happened" and "something happened" are decided by the bytes, not the clock.
"""

from __future__ import annotations

from ingress_helpers import ingress

from nexus_seed.adapters.file_watch import (
    FILE_CREATED,
    FILE_DELETED,
    FILE_MODIFIED,
    LocalFileAdapter,
    content_fingerprint,
)
from nexus_seed.runtime.runtime import Runtime


def watched(runtime, root):
    """Bind a file adapter to ``runtime``'s ingress service."""
    service = ingress(runtime)
    return LocalFileAdapter(root).bind(service), service


async def test_a_new_file_is_reported_as_created(tmp_path):
    """AT9."""
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    (root / "input.txt").write_text("first", encoding="utf-8")
    results = await adapter.poll_and_ingest()

    assert [r.accepted for r in results] == [True]
    events = runtime.event_store.all()
    assert len(events) == 1
    assert events[0].type == FILE_CREATED
    assert events[0].payload["path"] == "input.txt"
    assert events[0].payload["change_type"] == FILE_CREATED
    assert events[0].payload["content_hash"] == content_fingerprint(root / "input.txt")
    assert events[0].payload["size"] == 5
    assert events[0].payload["mtime"] is not None
    runtime.close()


async def test_a_changed_file_is_reported_as_modified(tmp_path):
    """AT10."""
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    target = root / "input.txt"
    target.write_text("first", encoding="utf-8")
    await adapter.poll_and_ingest()

    target.write_text("second", encoding="utf-8")
    await adapter.poll_and_ingest()

    types = [e.type for e in runtime.event_store.all()]
    assert types == [FILE_CREATED, FILE_MODIFIED]
    assert runtime.get_adapter_checkpoint("local_file", "input.txt").cursor == (
        content_fingerprint(target)
    )
    runtime.close()


async def test_polling_an_unchanged_tree_produces_nothing(tmp_path):
    """AT11: 'I looked again' is not an event."""
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    (root / "input.txt").write_text("stable", encoding="utf-8")
    await adapter.poll_and_ingest()

    for _ in range(3):
        assert await adapter.poll() == []
        assert await adapter.poll_and_ingest() == []

    assert len(runtime.event_store.all()) == 1
    runtime.close()


async def test_a_touch_that_changes_no_bytes_is_not_a_change(tmp_path):
    """Content, not mtime, decides (spec §36)."""
    import os
    import time

    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    target = root / "input.txt"
    target.write_text("same bytes", encoding="utf-8")
    await adapter.poll_and_ingest()

    future = time.time() + 120
    os.utime(target, (future, future))

    assert await adapter.poll() == []
    assert len(runtime.event_store.all()) == 1
    runtime.close()


async def test_reverting_a_file_is_a_change_back(tmp_path):
    """A→B→A is three versions; the middle one really happened."""
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    target = root / "input.txt"
    for content in ("A", "B", "A"):
        target.write_text(content, encoding="utf-8")
        await adapter.poll_and_ingest()

    types = [e.type for e in runtime.event_store.all()]
    assert types == [FILE_CREATED, FILE_MODIFIED, FILE_MODIFIED]
    runtime.close()


async def test_deletion_is_reported_once(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    target = root / "input.txt"
    target.write_text("gone soon", encoding="utf-8")
    await adapter.poll_and_ingest()

    target.unlink()
    await adapter.poll_and_ingest()
    await adapter.poll_and_ingest()  # deletion is not re-reported

    types = [e.type for e in runtime.event_store.all()]
    assert types == [FILE_CREATED, FILE_DELETED]
    deleted = runtime.event_store.all()[-1]
    assert deleted.payload["content_hash"] is None
    assert deleted.payload["path"] == "input.txt"
    runtime.close()


async def test_recreating_a_deleted_file_is_a_creation(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    target = root / "input.txt"
    target.write_text("v1", encoding="utf-8")
    await adapter.poll_and_ingest()
    target.unlink()
    await adapter.poll_and_ingest()
    target.write_text("v2", encoding="utf-8")
    await adapter.poll_and_ingest()

    assert [e.type for e in runtime.event_store.all()] == [
        FILE_CREATED,
        FILE_DELETED,
        FILE_CREATED,
    ]
    runtime.close()


async def test_deletion_detection_can_be_switched_off(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter = LocalFileAdapter(root, detect_deletions=False).bind(ingress(runtime))

    target = root / "input.txt"
    target.write_text("v1", encoding="utf-8")
    await adapter.poll_and_ingest()
    target.unlink()
    await adapter.poll_and_ingest()

    assert [e.type for e in runtime.event_store.all()] == [FILE_CREATED]
    runtime.close()


async def test_nested_files_are_watched_and_keyed_by_relative_path(tmp_path):
    root = tmp_path / "watched"
    (root / "a" / "b").mkdir(parents=True)
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    (root / "a" / "b" / "deep.txt").write_text("x", encoding="utf-8")
    await adapter.poll_and_ingest()

    assert runtime.event_store.all()[0].payload["path"] == "a/b/deep.txt"
    assert runtime.get_adapter_checkpoint("local_file", "a/b/deep.txt") is not None
    runtime.close()


async def test_the_adapter_does_not_interpret_content(tmp_path):
    """Invariant 34: it says a file changed, never what the file means."""
    root = tmp_path / "watched"
    root.mkdir()
    runtime = Runtime(tmp_path / "f.db")
    adapter, _ = watched(runtime, root)

    (root / "data.csv").write_text("entity,attribute,value\nD1_CD,target,45\n", encoding="utf-8")
    await adapter.poll_and_ingest()

    # A real change to world state is described in the file, and ignored.
    assert runtime.state_store.get("D1_CD", "target") is None
    assert not hasattr(runtime, "observation_store")
    assert not hasattr(runtime, "state_delta_store")
    payload = runtime.event_store.all()[0].payload
    assert set(payload) == {"path", "change_type", "size", "mtime", "content_hash"}
    runtime.close()


async def test_an_unbound_adapter_refuses_to_poll(tmp_path):
    import pytest

    from nexus_seed.adapters.base import AdapterError

    with pytest.raises(AdapterError):
        await LocalFileAdapter(tmp_path / "watched").poll()
