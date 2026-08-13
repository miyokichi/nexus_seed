"""Permission model — string capabilities a Process is granted.

Deliberately minimal (spec §14): flat strings, no hierarchy, no roles, no
RBAC engine.  A permission is granted to a *ProcessDefinition* (static, and
therefore reviewable) via its metadata::

    ProcessDefinition(
        name="write_analysis_result",
        ...,
        metadata={"permissions": ["filesystem.write"]},
    )

A running instance can never widen its own grant: it may only *declare* what it
needs on an ActionProposal, and declaring less than the backend mandates is
itself a validation failure (spec §17).
"""

from __future__ import annotations

from typing import Iterable

#: The permission strings Phase 3C knows about.  Not a closed set — validation
#: compares strings, so a new backend may introduce its own.
FILESYSTEM_READ = "filesystem.read"
FILESYSTEM_WRITE = "filesystem.write"
PROCESS_EXECUTE = "process.execute"
NETWORK_HTTP = "network.http"
REPOSITORY_READ = "repository.read"
REPOSITORY_WRITE = "repository.write"
SHELL_EXECUTE = "shell.execute"

KNOWN_PERMISSIONS: tuple[str, ...] = (
    FILESYSTEM_READ,
    FILESYSTEM_WRITE,
    PROCESS_EXECUTE,
    NETWORK_HTTP,
    REPOSITORY_READ,
    REPOSITORY_WRITE,
    SHELL_EXECUTE,
)

#: Metadata key on a ProcessDefinition holding its granted permissions.
PERMISSIONS_METADATA_KEY = "permissions"


def granted_permissions(definition) -> list[str]:
    """Return the permissions granted to a ``ProcessDefinition``.

    A definition with no ``permissions`` metadata is granted nothing — the
    default is *deny*, so forgetting to declare grants can never open a hole.
    """
    if definition is None:
        return []
    raw = (definition.metadata or {}).get(PERMISSIONS_METADATA_KEY)
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw]


def missing_permissions(required: Iterable[str], granted: Iterable[str]) -> list[str]:
    """Return the required permissions absent from ``granted`` (order kept)."""
    have = set(granted)
    return [p for p in dict.fromkeys(required) if p not in have]
