"""ResourceScope — one path boundary, shared by every component that has one.

Phase 3C's action backend and Phase 3D's file adapter each grew their own
``allowed_root`` check.  Two implementations of "is this path allowed" is one
too many: the second copy is where the symlink case gets forgotten.  This is
that check, once.

Scope is deliberately **only about paths** (spec §22).  It is not a sandbox: it
does not isolate processes, restrict the network, or model roles.  It answers
one question — may this component touch this path — and separates *read* from
*write*, because watching a directory and writing into it are different powers.
"""

from __future__ import annotations

from pathlib import Path


class ScopeViolation(ValueError):
    """A path fell outside the permitted roots.

    A ``ValueError`` subclass so existing callers that catch ``ValueError``
    around path resolution keep working.
    """


class ResourceScope:
    """Permitted read and write roots for file-like resources.

    Args:
        read_roots: Directories whose contents may be read.
        write_roots: Directories whose contents may be written.  Write access
            does not imply read access or vice versa; pass both when a
            component needs both.
    """

    def __init__(
        self,
        read_roots: list[str | Path] | None = None,
        write_roots: list[str | Path] | None = None,
        *,
        create: bool = True,
    ) -> None:
        self.read_roots = [self._prepare(r, create) for r in (read_roots or [])]
        self.write_roots = [self._prepare(r, create) for r in (write_roots or [])]

    @staticmethod
    def _prepare(root: str | Path, create: bool) -> Path:
        path = Path(root).resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def for_root(cls, root: str | Path, *, create: bool = True) -> "ResourceScope":
        """A scope where one directory is both readable and writable."""
        return cls([root], [root], create=create)

    @classmethod
    def read_only(cls, root: str | Path, *, create: bool = True) -> "ResourceScope":
        """A scope that may observe ``root`` but never write into it."""
        return cls([root], [], create=create)

    # --- queries -----------------------------------------------------------

    @property
    def allowed_roots(self) -> list[Path]:
        """Every root this scope touches, readable or writable."""
        merged = list(self.read_roots)
        merged.extend(r for r in self.write_roots if r not in merged)
        return merged

    def can_read(self, path: str | Path) -> bool:
        """Whether ``path`` resolves strictly inside a readable root."""
        return self._contained(path, self.read_roots)

    def can_write(self, path: str | Path) -> bool:
        """Whether ``path`` resolves strictly inside a writable root."""
        return self._contained(path, self.write_roots)

    def _contained(self, path: str | Path, roots: list[Path]) -> bool:
        if not roots:
            return False
        try:
            resolved = self._resolve_against(path, roots[0])
        except OSError:
            return False
        # Resolved, so a symlink pointing out of the tree is caught rather than
        # followed.  Strict containment: a root is never itself a target.
        return any(root in resolved.parents for root in roots)

    # --- resolution --------------------------------------------------------

    def resolve(self, path: str | Path, *, write: bool = False) -> Path:
        """Resolve ``path`` inside the scope.

        A relative path is taken relative to the first matching root, so
        callers can work in resource-relative terms.

        Raises:
            ScopeViolation: If ``path`` is empty or escapes the permitted roots.
        """
        if not path:
            raise ScopeViolation("path is required")
        roots = self.write_roots if write else self.read_roots
        if not roots:
            raise ScopeViolation(
                f"no {'writable' if write else 'readable'} roots configured"
            )
        try:
            resolved = self._resolve_against(path, roots[0])
        except OSError as exc:
            raise ScopeViolation(f"cannot resolve {path!r}: {exc}") from exc
        if not any(root in resolved.parents for root in roots):
            raise ScopeViolation(
                f"target {str(path)!r} escapes allowed_root {roots[0]}"
            )
        return resolved

    @staticmethod
    def _resolve_against(path: str | Path, default_root: Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = default_root / candidate
        return candidate.resolve()

    def relative_key(self, path: str | Path) -> str:
        """Return the root-relative, POSIX-style key for ``path``.

        The stable name a Resource is identified by, independent of where the
        watched tree happens to live on this machine.
        """
        resolved = self.resolve(path)
        for root in self.allowed_roots:
            if root in resolved.parents:
                return resolved.relative_to(root).as_posix()
        raise ScopeViolation(f"{path!r} is not under any root")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ResourceScope(read={[str(r) for r in self.read_roots]}, "
            f"write={[str(r) for r in self.write_roots]})"
        )
