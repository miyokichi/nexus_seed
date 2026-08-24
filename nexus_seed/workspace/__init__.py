"""Task workspaces: what an Agent may reach, and how it reaches it.

A Project Agent is delegated a goal, not a machine.  It needs more than its own
scratch directory — a shared project file, a document already in the Resource
store, something NEXUS SEED knows — but handing it the host filesystem to get
those is the wrong trade entirely.

So the answer is a *provisioned workspace*: NEXUS SEED decides which resources
this task may use and at what access, materialises exactly those into the
task's own directory, and hands over a directory the Agent can simply read.

    declare  ->  decide      ->  provide             ->  execute
    Task       GrantPolicy     WorkspaceProvisioner     Project Agent

Each step belongs to a different party and none of them is the Agent: the Agent
receives a workspace and works in it.  That is what keeps this generic — any
A2A agent, unmodified, can use a directory, so nothing here needs Hermes or
OpenCode to know what a grant is.
"""

from .models import (
    ACCESS_MODES,
    GRANTS_KEY,
    AccessMode,
    GrantDecision,
    GrantRequest,
    ResourceGrant,
    WorkspaceManifest,
    grants_in,
    with_grant,
)
from .policy import GrantPolicy, GrantRefused
from .provisioner import WorkspaceProvisioner

__all__ = [
    "ACCESS_MODES",
    "GRANTS_KEY",
    "AccessMode",
    "GrantDecision",
    "GrantPolicy",
    "GrantRefused",
    "GrantRequest",
    "ResourceGrant",
    "WorkspaceManifest",
    "WorkspaceProvisioner",
    "grants_in",
    "with_grant",
]
