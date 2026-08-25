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


class Delivery(str, Enum):
    """How a granted resource reaches the task.

    ``REFERENCE`` hands the Agent the real host path and nothing is copied:
    a read is a read of the original, and a write changes the original.  This
    is the ordinary case — most files a task consults do not want duplicating,
    and a directory cannot sensibly be duplicated at all.

    ``COPY`` materialises the resource inside the workspace.  It costs a copy
    and it is the only way to let an Agent edit something while the original
    stays untouched, so it stays the choice for anything that has to be
    isolated.  A read copy is never carried back; a read-write copy is,
    when the work is accepted.

    Neither is a security level on its own: both are bounded by the same
    authorized roots.  The difference is *whose bytes the Agent touches*.
    """

    REFERENCE = "reference"
    COPY = "copy"

    @property
    def copied(self) -> bool:
        return self is Delivery.COPY


DELIVERIES = tuple(item.value for item in Delivery)

#: What a grant with no recorded delivery means.  Copy, because that is what
#: every grant written before deliveries existed actually did — reading a
#: stored record must never change what it was.
STORED_DELIVERY_DEFAULT = Delivery.COPY

#: What a *new* grant means when nobody says.  Reference, because a task that
#: only needs to read a file should not be handed a duplicate of it.
NEW_DELIVERY_DEFAULT = Delivery.REFERENCE


def parse_delivery(value: Any, *, default: Delivery = STORED_DELIVERY_DEFAULT) -> Delivery:
    """Read a delivery mode, falling back to ``default`` for anything unknown."""
    try:
        return Delivery(str(value).strip().lower())
    except (AttributeError, ValueError):
        return default


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
        delivery: Whether the Agent reaches the original (``REFERENCE``) or a
            copy inside its workspace (``COPY``).  See :class:`Delivery`.
        granted_at: When the decision was made.
    """

    uri: str
    access: AccessMode = AccessMode.READ
    name: str = ""
    reason: str = ""
    delivery: Delivery = NEW_DELIVERY_DEFAULT
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
            "delivery": self.delivery.value,
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
            # No recorded delivery means the record predates deliveries, and
            # what it did then was copy.  Reading it must not change it.
            delivery=parse_delivery(data.get("delivery")),
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
    """What one task may reach, as the Agent will find it.

    Two kinds of entry, and the difference is deliberate:

    * a **copy** entry carries a workspace-relative ``path`` and no origin —
      the Agent is told what it has without being told where on the host it
      came from, because it is not going back there;
    * a **reference** entry carries the resolved host ``path``, because
      reaching the original *is* the grant.

    :attr:`readable_paths` and :attr:`writable_paths` are the reference
    entries only.  A copy already lives inside the workspace the Agent was
    given, so listing it again as a path to authorize would widen nothing and
    confuse what those two lists mean.
    """

    workspace: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "resources": list(self.entries),
            "readable_paths": self.readable_paths,
            "writable_paths": self.writable_paths,
        }

    def _referenced(self, access: AccessMode) -> list[str]:
        return [
            str(entry["path"])
            for entry in self.entries
            if entry.get("delivery") == Delivery.REFERENCE.value
            and entry.get("access") == access.value
        ]

    @property
    def readable_paths(self) -> list[str]:
        """Host paths the task may read in place."""
        return self._referenced(AccessMode.READ)

    @property
    def writable_paths(self) -> list[str]:
        """Host paths the task may change in place."""
        return self._referenced(AccessMode.READ_WRITE)

    @property
    def copied_writable_paths(self) -> list[str]:
        """Workspace-relative copies whose edits are carried back."""
        return [
            str(entry["path"])
            for entry in self.entries
            if entry.get("delivery") != Delivery.REFERENCE.value
            and entry.get("access") == AccessMode.READ_WRITE.value
        ]

    def narrowed_to(self, uris: list[str] | None) -> "WorkspaceManifest":
        """Return this manifest keeping only ``uris``.

        Narrowing only.  A uri that was never granted to the Project is not
        added by asking for it here, so a Task can restrict what it reaches
        but can never widen it.  ``None`` means "no narrowing asked for".
        """
        if uris is None:
            return self
        wanted = {str(item) for item in uris}
        return WorkspaceManifest(
            workspace=self.workspace,
            entries=[entry for entry in self.entries if entry.get("uri") in wanted],
        )


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


#: Where a Task says it needs less than its Project allows.  Narrowing only:
#: naming a uri here that the Project was never granted adds nothing.
TASK_RESOURCES_KEY = "resources"


def narrow_resources(
    resources: list[dict[str, Any]], task: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Return the manifest entries one Task may reach.

    A Task that says nothing reaches everything its Project was granted, which
    is the behaviour that predates this.  A Task naming ``context.resources``
    reaches only those — and only those it *already* had, because a Task cannot
    grant itself anything: the Project's grants are the ceiling.
    """
    wanted = ((task or {}).get("context") or {}).get(TASK_RESOURCES_KEY)
    if not isinstance(wanted, list):
        return list(resources)
    allowed = {str(item) for item in wanted}
    return [entry for entry in resources if entry.get("uri") in allowed]


def referenced_paths(resources: list[dict[str, Any]], access: str) -> list[str]:
    """Host paths among ``resources`` delivered by reference at ``access``.

    This is the conversion an Agent runtime actually needs: NEXUS SEED's
    grants on one side, ``readable_paths`` / ``writable_paths`` on the other.
    A copy is deliberately absent — it is inside the workspace the Agent was
    already given, so authorizing its path again would say nothing.
    """
    return [
        str(entry["path"])
        for entry in resources
        if entry.get("delivery") == Delivery.REFERENCE.value
        and entry.get("access") == access
    ]


def readable_grants(context: dict[str, Any] | None) -> list[ResourceGrant]:
    """A Project's ``readable_resources``: what it may read but not change."""
    return [item for item in grants_in(context) if not item.access.writable]


def writable_grants(context: dict[str, Any] | None) -> list[ResourceGrant]:
    """A Project's ``writable_resources``: what it may change."""
    return [item for item in grants_in(context) if item.access.writable]


__all__ = [
    "ACCESS_MODES",
    "DELIVERIES",
    "Delivery",
    "NEW_DELIVERY_DEFAULT",
    "STORED_DELIVERY_DEFAULT",
    "parse_delivery",
    "GRANTS_KEY",
    "TASK_RESOURCES_KEY",
    "grants_in",
    "narrow_resources",
    "readable_grants",
    "referenced_paths",
    "with_grant",
    "writable_grants",
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
