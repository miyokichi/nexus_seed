"""Path containment helpers shared by the tools and the A2A grant checks.

Kept dependency-free so both sides can import it without a cycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def is_within(root: Path, path: Path) -> bool:
    """True when ``path`` is ``root`` itself or lives under it.

    A root that exists and is *not* a directory (a single granted file) only
    ever matches itself.
    """

    if root == path:
        return True
    if root.exists() and not root.is_dir():
        return False
    return root in path.parents


def first_containing(roots: Iterable[Path], path: Path) -> Path | None:
    """The first root that contains ``path``, or ``None`` when none does."""

    for root in roots:
        if is_within(root, path):
            return root
    return None


def resolve_existing_parent(path: Path) -> Path:
    """The nearest ancestor of ``path`` (or ``path`` itself) that exists."""

    for parent in (path, *path.parents):
        if parent.exists():
            return parent.resolve()
    return path.resolve()
