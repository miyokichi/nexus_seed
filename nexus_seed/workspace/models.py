"""What a task may reach: grants, requests and the manifest an Agent reads.

These are domain records, not Core primitives — the same standing as
``Observation`` or ``Project``.  Nothing here executes anything or touches a
filesystem; :mod:`nexus_seed.workspace.policy` decides and
:mod:`nexus_seed.workspace.provisioner` provides.

A grant names *what* by URI rather than by host path, so the same declaration
survives being provisioned somewhere else::

    file:reports/2026-q3.xlsx      a path under an authorized root
    resource:<uri>                 something the Resource store already versions
    knowledge:<knowledge_id>       something NEXUS SEED was told
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow

#: Source schemes a grant may name.  Adding a kind is adding a materialiser in
#: the provisioner, not a new concept here.
SCHEME_FILE = "file"
SCHEME_RESOURCE = "resource"
SCHEME_KNOWLEDGE = "knowledge"
KNOWN_SCHEMES = (SCHEME_FILE, SCHEME_RESOURCE, SCHEME_KNOWLEDGE)


class AccessMode(str, Enum):
    """How much of a resource one task is allowed.

    Read is not a weaker write: a task granted ``READ`` gets a copy it cannot
    write back, so a mistake in the Agent cannot reach the original at all.
    ``READ_WRITE`` means edits are meant to be kept, and the provisioner is
    responsible for returning them.
    """

    READ = "read"
    READ_WRITE = "read_write"

    @property
    def writable(self) -> bool:
        return self is AccessMode.READ_WRITE


ACCESS_MODES = tuple(mode.value for mode in AccessMode)


def parse_access(value: Any, *, default: AccessMode = AccessMode.READ) -> AccessMode:
    """Read an access mode, falling back to the *less* powerful one.

    An unreadable or missing mode never grants more than it names.
    """
    try:
        return AccessMode(str(value).strip().lower())
    except (AttributeError, ValueError):
        return default


@dataclass(frozen=True)
class ResourceGrant:
    """One resource a task may use, and at what access.

    Attributes:
        uri: What is granted, as ``scheme:rest`` (see module docstring).
        access: How much of it this task gets.
        name: The file name it appears under inside the workspace.  Defaults
            to a name derived from ``uri``; set it when the Agent should see a
            particular name.
        reason: Why this was granted — kept for the audit trail, since a
            granted resource is a decision somebody made.
        granted_at: When the decision was made.
    """

    uri: str
    access: AccessMode = AccessMode.READ
    name: str = ""
    reason: str = ""
    granted_at: datetime = field(default_factory=utcnow)

    @property
    def scheme(self) -> str:
        """The part before the first colon, or ``""`` for a bare value."""
        return self.uri.split(":", 1)[0] if ":" in self.uri else ""

    @property
    def target(self) -> str:
        """Everything after the scheme."""
        return self.uri.split(":", 1)[1] if ":" in self.uri else self.uri

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "access": self.access.value,
            "name": self.name,
            "reason": self.reason,
            "granted_at": self.granted_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceGrant":
        granted_at = data.get("granted_at")
        return cls(
            uri=str(data.get("uri") or ""),
            access=parse_access(data.get("access")),
            name=str(data.get("name") or ""),
            reason=str(data.get("reason") or ""),
            granted_at=(
                datetime.fromisoformat(granted_at)
                if isinstance(granted_at, str) and granted_at
                else utcnow()
            ),
        )


@dataclass(frozen=True)
class GrantRequest:
    """A task asking for something it was not given.

    This is what a ``NEED_RESOURCE`` escalation becomes once it is read as a
    request rather than only a reason a Project stopped.  ``uri`` may be
    absent: an Agent that only knows it needs "last year's SAP export" is
    describing a need, and turning that into something grantable is a person's
    job, not a guess this record should make.
    """

    project_id: str
    requested: str
    reason: str = ""
    uri: str | None = None
    access: AccessMode = AccessMode.READ
    id: str = field(default_factory=lambda: f"grant-req-{uuid.uuid4().hex[:12]}")
    requested_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "requested": self.requested,
            "reason": self.reason,
            "uri": self.uri,
            "access": self.access.value,
            "requested_at": self.requested_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GrantRequest":
        return cls(
            project_id=str(data.get("project_id") or ""),
            requested=str(data.get("requested") or data.get("required_resource") or ""),
            reason=str(data.get("reason") or ""),
            uri=data.get("uri") or None,
            access=parse_access(data.get("access")),
            id=str(data.get("id") or f"grant-req-{uuid.uuid4().hex[:12]}"),
        )


@dataclass(frozen=True)
class GrantDecision:
    """Whether a request was allowed, and why."""

    allowed: bool
    reason: str
    grant: ResourceGrant | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "grant": self.grant.to_dict() if self.grant else None,
        }


@dataclass(frozen=True)
class WorkspaceManifest:
    """What was actually put in a workspace, as the Agent will find it.

    Handed over with the assignment so the Agent can be told what it has
    without being told where any of it came from on the host: entries carry
    the workspace-relative path and the access, never the origin path.
    """

    workspace: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"workspace": self.workspace, "resources": list(self.entries)}

    @property
    def writable_paths(self) -> list[str]:
        """Relative paths the task was allowed to change."""
        return [
            entry["path"]
            for entry in self.entries
            if entry.get("access") == AccessMode.READ_WRITE.value
        ]


#: Where a Project keeps what it has been granted.  Its ``context`` is already
#: durable and already travels with every delegation, so grants need no store
#: of their own — and a Project read back after a restart still knows what its
#: task may reach.
GRANTS_KEY = "resource_grants"


def grants_in(context: dict[str, Any] | None) -> list[ResourceGrant]:
    """Read the grants recorded on a Project's context."""
    raw = (context or {}).get(GRANTS_KEY)
    if not isinstance(raw, list):
        return []
    return [ResourceGrant.from_dict(item) for item in raw if isinstance(item, dict)]


def with_grant(
    context: dict[str, Any] | None, grant: ResourceGrant
) -> dict[str, Any]:
    """Return ``context`` with ``grant`` added, replacing one for the same uri.

    Re-granting a resource at a different access is a change of mind, not a
    second grant: keeping both would leave which one applies ambiguous.
    """
    updated = dict(context or {})
    kept = [item for item in grants_in(updated) if item.uri != grant.uri]
    updated[GRANTS_KEY] = [item.to_dict() for item in [*kept, grant]]
    return updated


__all__ = [
    "ACCESS_MODES",
    "GRANTS_KEY",
    "grants_in",
    "with_grant",
    "AccessMode",
    "GrantDecision",
    "GrantRequest",
    "KNOWN_SCHEMES",
    "ResourceGrant",
    "SCHEME_FILE",
    "SCHEME_KNOWLEDGE",
    "SCHEME_RESOURCE",
    "WorkspaceManifest",
    "parse_access",
]
