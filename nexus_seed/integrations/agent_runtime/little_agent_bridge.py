"""Translate NEXUS workspace grants to little_agent's public A2A metadata.

This module deliberately contains no little_agent import.  It translates wire
representations only; project decisions and execution remain on their existing
sides of the A2A boundary.
"""

from __future__ import annotations

from collections.abc import Iterable

LITTLE_AGENT_WORKSPACE_KEY = "littleAgent/workspace"
LITTLE_AGENT_ALLOWED_PATHS_KEY = "littleAgent/allowedPaths"


def little_agent_message_metadata(
    *, workspace: str | None, readable_paths: Iterable[str]
) -> dict[str, object]:
    """Return the message-level WorkGrant metadata understood by little_agent.

    A little_agent workspace is already read/write.  NEXUS writable resource
    copies live inside that workspace, so only external read-only references
    are carried as ``allowedPaths``.
    """

    metadata: dict[str, object] = {}
    if workspace:
        metadata[LITTLE_AGENT_WORKSPACE_KEY] = workspace
    paths = list(dict.fromkeys(path for path in readable_paths if path))
    if paths:
        metadata[LITTLE_AGENT_ALLOWED_PATHS_KEY] = paths
    return metadata


__all__ = [
    "LITTLE_AGENT_ALLOWED_PATHS_KEY",
    "LITTLE_AGENT_WORKSPACE_KEY",
    "little_agent_message_metadata",
]
