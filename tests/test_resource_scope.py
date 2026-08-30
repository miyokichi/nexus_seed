"""AT13 (spec §65, §21–§22): one path boundary, shared by both directions.

Phase 3C's action backend and Phase 3D's file adapter each had their own
``allowed_root`` check.  Two implementations of "is this path allowed" is one
too many — the second copy is where the symlink case gets forgotten.  This is
that check, once, with read and write kept distinct.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from resource_helpers import resource_runtime, watched_tree

from nexus_seed.adapters.file_watch import LocalFileAdapter
from nexus_seed.resources.scope import ResourceScope, ScopeViolation
from nexus_seed.runtime.runtime import Runtime


def test_containment_is_strict_and_resolved(tmp_path):
    root = tmp_path / "root"
    scope = ResourceScope.for_root(root)

    assert scope.can_read(root / "a.txt")
    assert scope.can_read(root / "nested" / "b.txt")
    assert not scope.can_read(tmp_path / "outside.txt")
    assert not scope.can_read(root / ".." / "escape.txt")
    # The root itself is never a target.
    assert not scope.can_read(root)


def test_read_and_write_are_separate_powers(tmp_path):
    read_only = ResourceScope.read_only(tmp_path / "watched")
    assert read_only.can_read(tmp_path / "watched" / "a.txt")
    assert not read_only.can_write(tmp_path / "watched" / "a.txt")

    with pytest.raises(ScopeViolation):
        read_only.resolve("a.txt", write=True)


def test_resolve_rejects_escapes_and_empty_paths(tmp_path):
    scope = ResourceScope.for_root(tmp_path / "root")
    for target in ("../escape.txt", "a/../../escape.txt", str(tmp_path / "outside.txt"), ""):
        with pytest.raises(ScopeViolation):
            scope.resolve(target)


def test_a_scope_violation_is_still_a_value_error(tmp_path):
    """Callers written against the old per-component check keep working."""
    scope = ResourceScope.for_root(tmp_path / "root")
    with pytest.raises(ValueError):
        scope.resolve("../escape.txt")


def test_relative_paths_resolve_against_the_root(tmp_path):
    root = tmp_path / "root"
    scope = ResourceScope.for_root(root)
    assert scope.resolve("a/b.txt") == (root / "a" / "b.txt").resolve()
    assert scope.relative_key(root / "a" / "b.txt") == "a/b.txt"


def test_multiple_roots_are_all_permitted(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    scope = ResourceScope([a, b], [b])
    assert scope.can_read(a / "x.txt") and scope.can_read(b / "x.txt")
    assert scope.can_write(b / "x.txt") and not scope.can_write(a / "x.txt")
    assert len(scope.allowed_roots) == 2


def test_an_empty_scope_permits_nothing(tmp_path):
    scope = ResourceScope()
    assert not scope.can_read(tmp_path / "anything")
    assert not scope.can_write(tmp_path / "anything")


def test_the_file_adapter_watches_read_only(tmp_path):
    adapter = LocalFileAdapter(tmp_path / "watched")
    assert isinstance(adapter.scope, ResourceScope)
    assert adapter.scope.can_read(tmp_path / "watched" / "a.txt")
    # A watcher observes; it has no business writing what it watches.
    assert not adapter.scope.can_write(tmp_path / "watched" / "a.txt")


async def test_an_out_of_scope_path_is_never_catalogued(tmp_path):
    """AT13: a file event naming a path outside the roots creates no Resource."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    resource_runtime(runtime, root)

    from nexus_seed.core.event import Event

    await runtime.submit_event(
        Event(
            "file_created",
            "local_file",
            {
                "path": "../../etc/passwd",
                "change_type": "file_created",
                "content_hash": "sha256-forged",
                "size": 1,
                "mtime": None,
            },
        )
    )

    assert runtime.get_resources() == []
    assert runtime.event_store.by_type("resource_version_created") == []
    indexer = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "resource_indexer"
    ][0]
    assert indexer.local_state["output"]["indexed"] is False
    assert "escapes allowed_root" in indexer.local_state["output"]["reason"]
    runtime.close()


async def test_a_symlink_out_of_the_tree_is_not_readable(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    scope = ResourceScope.read_only(root)
    assert not scope.can_read(root / "link.txt")
    with pytest.raises(ScopeViolation):
        scope.resolve("link.txt")


def test_scope_repr_names_its_roots(tmp_path):
    text = repr(ResourceScope.for_root(tmp_path / "root"))
    assert "ResourceScope(read=" in text and "root" in text


def test_nonexistent_roots_can_be_declared_without_creating_them(tmp_path):
    """Scope answers containment, not existence — a file may appear later."""
    missing = tmp_path / "not-there"
    scope = ResourceScope.read_only(missing, create=False)

    assert not Path(missing).exists()
    assert scope.can_read(missing / "a.txt")
    assert not scope.can_read(tmp_path / "elsewhere.txt")
