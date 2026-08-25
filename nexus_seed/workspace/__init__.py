"""Task workspaces: what an Agent may reach, and how it reaches it.

A Project Agent is delegated a goal, not a machine.  It needs more than its own
scratch directory — a shared project file, a document already in the Resource
store, something NEXUS SEED knows — but handing it the host filesystem to get
those is the wrong trade entirely.

So the answer is a *provisioned workspace*: NEXUS SEED decides which resources
this task may use and at what access, provides exactly those, and hands over a
workspace plus the paths that go with it.

    declare  ->  decide      ->  provide             ->  execute
    Task       GrantPolicy     WorkspaceProvisioner     Project Agent

Each step belongs to a different party and none of them is the Agent: the Agent
receives a workspace and works in it.  That is what keeps this generic — any
A2A agent, unmodified, can use a directory and a list of paths, so nothing here
needs Hermes or OpenCode to know what a grant is.

Providing happens one of two ways, per grant (:class:`Delivery`):

* **reference** — the Agent is pointed at the real path and nothing is copied.
  The ordinary case, and the only one that works for a directory.
* **copy** — materialised inside the workspace, so an Agent may edit it while
  the original stays untouched.  Kept for exactly that: isolation.
"""

from .models import (
    ACCESS_MODES,
    DELIVERIES,
    GRANTS_KEY,
    NEW_DELIVERY_DEFAULT,
    STORED_DELIVERY_DEFAULT,
    AccessMode,
    Delivery,
    GrantDecision,
    GrantRequest,
    ResourceGrant,
    WorkspaceManifest,
    TASK_RESOURCES_KEY,
    grants_in,
    narrow_resources,
    parse_delivery,
    readable_grants,
    referenced_paths,
    with_grant,
    writable_grants,
)
from .policy import GrantPolicy, GrantRefused
from .provisioner import WorkspaceProvisioner

__all__ = [
    "ACCESS_MODES",
    "DELIVERIES",
    "GRANTS_KEY",
    "NEW_DELIVERY_DEFAULT",
    "STORED_DELIVERY_DEFAULT",
    "AccessMode",
    "Delivery",
    "GrantDecision",
    "GrantPolicy",
    "GrantRefused",
    "GrantRequest",
    "ResourceGrant",
    "WorkspaceManifest",
    "WorkspaceProvisioner",
    "TASK_RESOURCES_KEY",
    "grants_in",
    "narrow_resources",
    "parse_delivery",
    "readable_grants",
    "referenced_paths",
    "with_grant",
    "writable_grants",
]
