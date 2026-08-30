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

How a resource is provided is not a choice — it follows from the access:

* **read** — the Agent is pointed at the real path and nothing is copied.
  Cheap, current, and the only thing that works for a directory.
* **write** — copied into the workspace.  The Agent edits its own copy and
  the original is untouched until the work is accepted, which is the one
  moment the edits arrive.
"""

from .models import (
    ACCESS_MODES,
    DELIVERIES,
    GRANTS_KEY,
    AccessMode,
    Delivery,
    GrantDecision,
    GrantRequest,
    ResourceGrant,
    WorkspaceManifest,
    TASK_RESOURCES_KEY,
    authorized_paths,
    delivery_for,
    grants_in,
    narrow_resources,
    readable_grants,
    with_grant,
    writable_grants,
)
from .policy import GrantPolicy, GrantRefused
from .provisioner import WorkspaceProvisioner

__all__ = [
    "ACCESS_MODES",
    "DELIVERIES",
    "GRANTS_KEY",
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
    "authorized_paths",
    "delivery_for",
    "grants_in",
    "narrow_resources",
    "readable_grants",
    "with_grant",
    "writable_grants",
]
