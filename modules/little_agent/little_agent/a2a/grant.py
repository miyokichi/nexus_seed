"""Work grants: where a delegated A2A task is allowed to work.

A caller hands work over with ``delegate_task``; a grant says *where* that work
happens — which directory the peer treats as its workspace for the task, and
which files or directories outside it the peer may read. The grant travels in
the A2A message metadata under namespaced keys, so a peer that does not
understand it simply ignores it and runs in its own workspace.

The two halves convey different access, and are checked against different roots:

===============  ==============  ==========================================
part             access          must be within
===============  ==============  ==========================================
``workspace``    read + write    the deciding agent's **writable** roots
``allowed_paths``  read only     the deciding agent's **readable** roots
===============  ==============  ==========================================

So an agent that may only read a shared reference folder can still hand it to a
peer as an allowed path, but cannot make it anyone's workspace.

A grant is a **request, never an authorization**, and the same check runs twice:

1. The caller checks what it is about to hand over against its own roots, so an
   agent cannot widen its own reach by delegating.
2. The server checks what arrived against its own, refusing anything outside it
   with ``-32602 InvalidParams``.

A server with no policy refuses every grant and works only in its own workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from little_agent.config import AgentConfig
from little_agent.paths import is_within

# Metadata keys carrying the grant between Little Agent peers.
WORKSPACE_METADATA_KEY = "littleAgent/workspace"
ALLOWED_PATHS_METADATA_KEY = "littleAgent/allowedPaths"


class GrantError(ValueError):
    """A requested workspace or path that may not be handed out or accepted."""


def _clean(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


@dataclass(frozen=True, slots=True)
class WorkGrant:
    """Where a delegated task should work, as requested by the caller.

    The two halves carry different access. ``workspace`` is where the peer
    works, so it is read **and** write. ``allowed_paths`` are reference material
    outside it — read **only**, so handing a peer a price list cannot end with
    the peer rewriting it. Output belongs in the granted workspace.
    """

    workspace: Path | None = None
    allowed_paths: tuple[Path, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.workspace is None and not self.allowed_paths

    def to_metadata(self) -> dict[str, Any]:
        """The metadata entries to attach to an A2A message (empty when unset)."""

        metadata: dict[str, Any] = {}
        if self.workspace is not None:
            metadata[WORKSPACE_METADATA_KEY] = str(self.workspace)
        if self.allowed_paths:
            metadata[ALLOWED_PATHS_METADATA_KEY] = [str(path) for path in self.allowed_paths]
        return metadata

    @classmethod
    def from_metadata(cls, metadata: Any) -> "WorkGrant":
        """Read a grant off an incoming message; anything malformed is ignored."""

        if not isinstance(metadata, dict):
            return cls()
        workspace = _clean(metadata.get(WORKSPACE_METADATA_KEY))
        raw_paths = metadata.get(ALLOWED_PATHS_METADATA_KEY)
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        paths: list[Path] = []
        if isinstance(raw_paths, list):
            for entry in raw_paths:
                path = _clean(entry)
                if path is not None and path not in paths:
                    paths.append(path)
        return cls(workspace=workspace, allowed_paths=tuple(paths))

    def resolve(self, base: Path) -> "WorkGrant":
        """Absolute form of this grant, taking relative entries from ``base``."""

        def absolute(path: Path) -> Path:
            return (path if path.is_absolute() else base / path).resolve()

        return WorkGrant(
            workspace=absolute(self.workspace) if self.workspace is not None else None,
            allowed_paths=tuple(dict.fromkeys(absolute(path) for path in self.allowed_paths)),
        )

    @classmethod
    def request(
        cls,
        base: Path,
        workspace: Any = None,
        allowed_paths: Any = None,
    ) -> "WorkGrant":
        """Build a grant from raw tool arguments, resolved against ``base``.

        Raises :class:`GrantError` when ``allowed_paths`` is not a list of paths.
        """

        if isinstance(allowed_paths, str):
            allowed_paths = [allowed_paths]
        if allowed_paths is None:
            allowed_paths = []
        if not isinstance(allowed_paths, list):
            raise GrantError("allowed_paths must be an array of file or directory paths.")
        paths: list[Path] = []
        for entry in allowed_paths:
            path = _clean(entry)
            if path is None:
                raise GrantError("allowed_paths entries must be non-empty paths.")
            paths.append(path)
        return cls(workspace=_clean(workspace), allowed_paths=tuple(paths)).resolve(base)

    def apply(self, config: AgentConfig) -> AgentConfig:
        """Layer the grant onto a config: each field only when the grant sets it.

        The workspace replaces ``config.workspace``, so relative tool paths and
        the task's writes follow the delegated directory. The granted paths
        replace ``config.readable_paths``, which makes them readable and leaves
        them unwritable — the server's own ``writable_paths`` are untouched.
        """

        if self.is_empty:
            return config
        return replace(
            config,
            workspace=self.workspace.resolve() if self.workspace is not None else config.workspace,
            readable_paths=(
                tuple(path.resolve() for path in self.allowed_paths)
                if self.allowed_paths
                else config.readable_paths
            ),
        )


@dataclass(frozen=True, slots=True)
class GrantPolicy:
    """Decides which requested paths one side may hand out or accept.

    Two root sets, because the two halves of a grant convey different access:
    ``workspace`` is where the peer *works*, so it needs write; ``allowed_paths``
    are references the peer may only read. A path this agent can merely read is
    therefore grantable as an allowed path but never as a workspace.

    The same policy runs on both sides — the caller checks what it is about to
    hand over against its own roots, and the server checks what arrived against
    its own. ``allow_any`` lifts both checks for a deliberately open, private
    server.
    """

    writable_roots: tuple[Path, ...] = ()
    readable_roots: tuple[Path, ...] = ()
    allow_any: bool = False

    def __post_init__(self) -> None:
        # Whatever is writable is also readable, so the read check never has to
        # consult both lists.
        merged = tuple(dict.fromkeys((*self.writable_roots, *self.readable_roots)))
        object.__setattr__(self, "readable_roots", merged)

    @classmethod
    def from_config(cls, config: AgentConfig, allow_any: bool = False) -> "GrantPolicy":
        return cls(
            writable_roots=(
                config.workspace.resolve(),
                *(path.resolve() for path in config.writable_paths),
            ),
            readable_roots=tuple(path.resolve() for path in config.readable_paths),
            allow_any=allow_any,
        )

    def _check(self, label: str, path: Path, roots: tuple[Path, ...], access: str) -> Path:
        resolved = path.expanduser().resolve()
        if self.allow_any:
            return resolved
        if any(is_within(root, resolved) for root in roots):
            return resolved
        known = ", ".join(str(root) for root in roots) or "(none)"
        raise GrantError(
            f"{label} is outside the {access} paths: {path} ({access} roots: {known})"
        )

    def authorize(self, grant: WorkGrant) -> WorkGrant:
        """Return the grant with absolute paths, or raise :class:`GrantError`."""

        if grant.is_empty:
            return grant
        workspace = (
            self._check("workspace", grant.workspace, self.writable_roots, "writable")
            if grant.workspace is not None
            else None
        )
        paths = tuple(
            self._check("allowed path", path, self.readable_roots, "readable")
            for path in grant.allowed_paths
        )
        return WorkGrant(workspace=workspace, allowed_paths=paths)
