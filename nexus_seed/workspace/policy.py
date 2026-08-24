"""GrantPolicy — whether a task may reach a resource, decided by NEXUS SEED.

The whole point of provisioning a workspace is that the Agent does not choose
what it can see.  This is where that choice is actually made, and it is
deliberately deterministic: no LLM decides what a task may read, because a
model that can be talked into widening its own access is not a boundary.

Two rules, and nothing else:

* **A path must be inside an authorized root.**  Reuses
  :class:`~nexus_seed.resources.scope.ResourceScope`, which already resolves
  symlinks before deciding — so a link pointing out of the tree is refused
  rather than followed.
* **Write is granted separately from read.**  A root being readable says
  nothing about whether a task may change what is in it, which is why the
  scope keeps two lists.

Everything else is closed by default.  A scheme this policy does not know, a
path outside every root, a write into a read-only root: refused, with a reason
worth showing a person.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..resources.scope import ResourceScope, ScopeViolation
from .models import (
    SCHEME_FILE,
    SCHEME_KNOWLEDGE,
    SCHEME_RESOURCE,
    AccessMode,
    GrantDecision,
    GrantRequest,
    ResourceGrant,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..knowledge.ledger import KnowledgeLedger
    from ..resources.service import ResourceService

logger = logging.getLogger("nexus_seed.workspace.policy")


class GrantRefused(PermissionError):
    """A resource was asked for that this policy will not grant."""


class GrantPolicy:
    """Decides which resources a task may be given, and at what access.

    Args:
        scope: The path boundary for ``file:`` grants.  Without one, no file
            is grantable at all — the safe default is that NEXUS SEED shares
            nothing from the host until an operator says which tree it may.
        resources: Resource service, if ``resource:`` grants are allowed.
        ledger: Knowledge ledger, if ``knowledge:`` grants are allowed.
        allow_knowledge_write: Knowledge is an append-only record of what was
            observed, so handing a task a writable copy of one would invite an
            edit that can never be honoured.  Off unless deliberately enabled.
    """

    def __init__(
        self,
        *,
        scope: ResourceScope | None = None,
        resources: "ResourceService | None" = None,
        ledger: "KnowledgeLedger | None" = None,
        allow_knowledge_write: bool = False,
    ) -> None:
        self.scope = scope
        self.resources = resources
        self.ledger = ledger
        self.allow_knowledge_write = allow_knowledge_write

    # --- deciding ----------------------------------------------------------

    def decide(self, request: GrantRequest) -> GrantDecision:
        """Allow or refuse one request, with the reason either way."""
        uri = (request.uri or "").strip()
        if not uri:
            return GrantDecision(
                False,
                "the request names no resource NEXUS SEED can identify; "
                "a person has to say which resource this is",
            )
        try:
            self.check(uri, request.access)
        except GrantRefused as exc:
            return GrantDecision(False, str(exc))
        return GrantDecision(
            True,
            f"{request.access.value} access to {uri} is within policy",
            ResourceGrant(
                uri=uri,
                access=request.access,
                reason=request.reason or f"requested: {request.requested}",
            ),
        )

    def check(self, uri: str, access: AccessMode) -> None:
        """Raise :class:`GrantRefused` unless ``uri`` may be granted at ``access``."""
        scheme, _, target = uri.partition(":")
        if not target:
            raise GrantRefused(f"{uri!r} does not name a scheme; expected 'scheme:target'")
        if scheme == SCHEME_FILE:
            self._check_file(target, access)
        elif scheme == SCHEME_RESOURCE:
            self._check_resource(target, access)
        elif scheme == SCHEME_KNOWLEDGE:
            self._check_knowledge(target, access)
        else:
            raise GrantRefused(f"unsupported resource scheme {scheme!r}")

    def permitted(self, grants: list[ResourceGrant]) -> list[ResourceGrant]:
        """Return only the grants this policy still allows.

        Re-checked at provisioning time, not just when granted: a root can be
        narrowed after the fact, and a stored grant must not outlive the
        permission it was made under.
        """
        allowed = []
        for grant in grants:
            try:
                self.check(grant.uri, grant.access)
            except GrantRefused as exc:
                logger.warning("dropping grant %s: %s", grant.uri, exc)
                continue
            allowed.append(grant)
        return allowed

    # --- per-scheme rules ---------------------------------------------------

    def _check_file(self, target: str, access: AccessMode) -> None:
        if self.scope is None:
            raise GrantRefused(
                "no authorized file root is configured, so no host file is grantable"
            )
        try:
            resolved = self.scope.resolve(target, write=access.writable)
        except ScopeViolation as exc:
            raise GrantRefused(str(exc)) from exc
        if not Path(resolved).exists():
            raise GrantRefused(f"{target!r} does not exist under the authorized root")

    def _check_resource(self, target: str, access: AccessMode) -> None:
        if self.resources is None:
            raise GrantRefused("the Resource store is not available to this policy")
        if access.writable:
            raise GrantRefused(
                "a Resource version is immutable; grant the underlying file "
                "with file: if the task is meant to change it"
            )
        if self.resources.get_resource_by_uri(target) is None:
            raise GrantRefused(f"no Resource is known by uri {target!r}")

    def _check_knowledge(self, target: str, access: AccessMode) -> None:
        if self.ledger is None:
            raise GrantRefused("the Knowledge Ledger is not available to this policy")
        if access.writable and not self.allow_knowledge_write:
            raise GrantRefused(
                "Knowledge is an append-only record; a task cannot be given a "
                "writable copy of one"
            )
        if self.ledger.head(target) is None:
            raise GrantRefused(f"no Knowledge is known by id {target!r}")


__all__ = ["GrantPolicy", "GrantRefused"]
