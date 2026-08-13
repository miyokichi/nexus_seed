"""AT12 (spec §62): a restart must not re-observe what was already ingested.

Two defences, and the test proves both are load-bearing:

* the **checkpoint** tells a rebuilt adapter which version of each file it had
  already seen, so an existing file is not misread as newly created;
* **ingress dedup** catches anything the checkpoint misses, because the
  source_event_key includes the content hash.
"""

from __future__ import annotations

from ingress_helpers import ingress

from nexus_seed.adapters.file_watch import (
    FILE_CREATED,
    FILE_MODIFIED,
    LocalFileAdapter,
    content_fingerprint,
)
from nexus_seed.runtime.runtime import Runtime


def rebuild(db_path, root):
    """Build a fresh Runtime + adapter over the same database and directory."""
    runtime = Runtime(db_path)
    adapter = LocalFileAdapter(root).bind(ingress(runtime))
    return runtime, adapter


async def test_rescanning_after_a_restart_creates_no_events(tmp_path):
    db_path = tmp_path / "restart.db"
    root = tmp_path / "watched"
    root.mkdir()

    runtime, adapter = rebuild(db_path, root)
    (root / "a.txt").write_text("v1", encoding="utf-8")
    (root / "b.txt").write_text("v1", encoding="utf-8")
    await adapter.poll_and_ingest()
    assert len(runtime.event_store.all()) == 2
    runtime.close()

    # --- everything in memory is discarded; the files are untouched ---
    runtime2, adapter2 = rebuild(db_path, root)

    assert await adapter2.poll() == []
    await adapter2.poll_and_ingest()
    assert len(runtime2.event_store.all()) == 2
    runtime2.close()


async def test_a_change_made_while_stopped_is_picked_up_once(tmp_path):
    db_path = tmp_path / "restart.db"
    root = tmp_path / "watched"
    root.mkdir()

    runtime, adapter = rebuild(db_path, root)
    target = root / "a.txt"
    target.write_text("v1", encoding="utf-8")
    await adapter.poll_and_ingest()
    runtime.close()

    # The world moves on while nothing is watching.
    target.write_text("v2", encoding="utf-8")

    runtime2, adapter2 = rebuild(db_path, root)
    await adapter2.poll_and_ingest()

    types = [e.type for e in runtime2.event_store.all()]
    assert types == [FILE_CREATED, FILE_MODIFIED]

    # A second poll after the restart adds nothing.
    await adapter2.poll_and_ingest()
    assert len(runtime2.event_store.all()) == 2
    assert runtime2.get_adapter_checkpoint("local_file", "a.txt").cursor == (
        content_fingerprint(target)
    )
    runtime2.close()


async def test_dedup_still_protects_when_the_checkpoint_is_lost(tmp_path):
    """Belt and braces: wipe the checkpoints and rescan — still no new events."""
    db_path = tmp_path / "restart.db"
    root = tmp_path / "watched"
    root.mkdir()

    runtime, adapter = rebuild(db_path, root)
    (root / "a.txt").write_text("v1", encoding="utf-8")
    await adapter.poll_and_ingest()
    original_event_id = runtime.event_store.all()[0].id
    runtime.close()

    runtime2, adapter2 = rebuild(db_path, root)
    runtime2.adapter_checkpoint_store.delete("local_file", "a.txt")

    # The adapter now believes the file is new and re-offers it...
    envelopes = await adapter2.poll()
    assert [e.event_type for e in envelopes] == [FILE_CREATED]

    # ...and ingress recognises the content hash it has already seen.
    results = await adapter2.poll_and_ingest()
    assert [r.duplicate for r in results] == [True]
    assert len(runtime2.event_store.all()) == 1
    assert runtime2.event_store.all()[0].id == original_event_id
    runtime2.close()


async def test_a_partial_batch_resumes_from_where_it_stopped(tmp_path):
    """A crash mid-batch leaves the unprocessed files still pending."""
    db_path = tmp_path / "partial.db"
    root = tmp_path / "watched"
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text("v1", encoding="utf-8")

    runtime, adapter = rebuild(db_path, root)
    service = adapter.checkpoints
    envelopes = await adapter.poll()
    assert len(envelopes) == 3

    # Only the first is ingested before the process "dies".
    await service.ingest(envelopes[0], checkpoint=adapter.checkpoint_for(envelopes[0]))
    runtime.close()

    runtime2, adapter2 = rebuild(db_path, root)
    remaining = await adapter2.poll()
    assert sorted(e.payload["path"] for e in remaining) == ["b.txt", "c.txt"]

    await adapter2.poll_and_ingest()
    assert len(runtime2.event_store.all()) == 3
    assert len(runtime2.get_adapter_checkpoints("local_file")) == 3
    runtime2.close()
