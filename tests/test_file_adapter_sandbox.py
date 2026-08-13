"""AT13 (spec §63, §38): the watcher cannot be pointed outside its root.

An observation boundary needs a sandbox for the same reason the action boundary
does — reading arbitrary paths is how a watched directory turns into an
exfiltration channel.  Enforced by resolving paths, so a symlink is caught
rather than followed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ingress_helpers import ingress

from nexus_seed.adapters.file_watch import LocalFileAdapter
from nexus_seed.runtime.runtime import Runtime


def test_paths_inside_the_root_are_accepted(tmp_path):
    root = tmp_path / "watched"
    adapter = LocalFileAdapter(root)
    assert adapter.within_sandbox(root / "a.txt")
    assert adapter.within_sandbox(root / "nested" / "b.txt")


def test_paths_outside_the_root_are_refused(tmp_path):
    root = tmp_path / "watched"
    adapter = LocalFileAdapter(root)
    assert not adapter.within_sandbox(tmp_path / "outside.txt")
    assert not adapter.within_sandbox(root / ".." / "escape.txt")
    assert not adapter.within_sandbox(Path(tmp_path.anchor) / "etc" / "passwd")


async def test_a_file_outside_the_root_is_never_ingested(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours", encoding="utf-8")

    runtime = Runtime(tmp_path / "sandbox.db")
    adapter = LocalFileAdapter(root).bind(ingress(runtime))

    (root / "mine.txt").write_text("fine", encoding="utf-8")
    await adapter.poll_and_ingest()

    paths = [e.payload["path"] for e in runtime.event_store.all()]
    assert paths == ["mine.txt"]
    runtime.close()


async def test_a_symlink_escaping_the_root_is_not_followed(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours", encoding="utf-8")

    link = root / "sneaky.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    runtime = Runtime(tmp_path / "sandbox.db")
    adapter = LocalFileAdapter(root).bind(ingress(runtime))

    assert not adapter.within_sandbox(link)
    await adapter.poll_and_ingest()
    assert runtime.event_store.all() == []
    runtime.close()


async def test_a_symlinked_directory_escaping_the_root_is_not_followed(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("not yours", encoding="utf-8")

    try:
        (root / "linked").symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    runtime = Runtime(tmp_path / "sandbox.db")
    adapter = LocalFileAdapter(root).bind(ingress(runtime))

    await adapter.poll_and_ingest()
    assert runtime.event_store.all() == []
    runtime.close()


async def test_the_scan_survives_an_unreadable_file(tmp_path):
    """One bad file must not blind the whole observation."""
    root = tmp_path / "watched"
    root.mkdir()
    (root / "good.txt").write_text("ok", encoding="utf-8")

    runtime = Runtime(tmp_path / "sandbox.db")
    adapter = LocalFileAdapter(root).bind(ingress(runtime))

    real_fingerprint = adapter.__class__.__module__
    import nexus_seed.adapters.file_watch as module

    original = module.content_fingerprint
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if path.name == "bad.txt":
            raise OSError("permission denied")
        return original(path)

    (root / "bad.txt").write_text("unreadable", encoding="utf-8")
    module.content_fingerprint = flaky
    try:
        await adapter.poll_and_ingest()
    finally:
        module.content_fingerprint = original

    assert real_fingerprint  # module import sanity
    assert calls["n"] >= 2
    assert [e.payload["path"] for e in runtime.event_store.all()] == ["good.txt"]
    runtime.close()
