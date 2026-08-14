"""ResourceService — the rules for turning an observation into a version.

Read-and-decide logic, deliberately separated from both the store (which only
persists) and the Process (which only stages effects).  The decisions it owns:

* is this URI a Resource we already know, or a new one?
* is this content a *new version*, or the version we already hold?
* what number does a new version get?

The last two are what stop a watcher that re-reads an unchanged file from
growing a version per poll (spec §11).

It reads through the store but never writes: everything it produces is returned
for a handler to stage on its ProcessResult, so resource writes stay inside the
Phase 2A atomic transition like every other effect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from .extractors import resource_type_for
from .models import Resource, ResourceVersion, content_hash
from .scope import ResourceScope, ScopeViolation


@dataclass
class IndexResult:
    """What indexing one observed file version implies.

    Attributes:
        resource: The Resource, existing or newly built.
        version: The new version, or ``None`` if the content is unchanged.
        resource_is_new: Whether ``resource`` still has to be persisted.
        unchanged_version: The version already holding this content, when the
            content turned out to be one we have.
        reason: Why nothing new was produced, when nothing was.
    """

    resource: Resource | None = None
    version: ResourceVersion | None = None
    resource_is_new: bool = False
    unchanged_version: ResourceVersion | None = None
    reason: str | None = None

    @property
    def created_version(self) -> bool:
        """Whether this observation produced a genuinely new version."""
        return self.version is not None


def file_uri(scope: ResourceScope, path: str | Path) -> str:
    """Build the stable URI a watched file is known by.

    Root-relative, so the same file keeps one identity regardless of where the
    watched tree lives on a given machine.
    """
    return f"file:///{scope.relative_key(path)}"


class ResourceService:
    """Decides what a newly observed piece of content means for a Resource."""

    def __init__(self, store, *, scope: ResourceScope | None = None) -> None:
        self.store = store
        self.scope = scope

    # --- reads (also exposed to handlers via ctx.services) -----------------

    def get_resource(self, resource_id: uuid.UUID) -> Resource | None:
        """Return a Resource by id."""
        return self.store.get_resource(resource_id)

    def get_resource_by_uri(self, uri: str) -> Resource | None:
        """Return a Resource by its URI."""
        return self.store.get_resource_by_uri(uri)

    def get_current_version(self, resource_id: uuid.UUID) -> ResourceVersion | None:
        """Return the newest version of a Resource."""
        return self.store.get_current_version(resource_id)

    def get_versions(self, resource_id: uuid.UUID) -> list[ResourceVersion]:
        """Return every version of a Resource, oldest first."""
        return self.store.get_versions(resource_id)

    # --- content -----------------------------------------------------------

    def read_bytes(self, locator: str) -> bytes:
        """Read a version's content through the scope.

        Raises:
            ScopeViolation: If ``locator`` is outside the readable roots.
            OSError: If the content cannot be read.
        """
        if self.scope is None:
            raise ScopeViolation("no ResourceScope configured for reading")
        return self.scope.resolve(locator).read_bytes()

    # --- indexing ----------------------------------------------------------

    def index_observation(
        self,
        *,
        uri: str,
        locator: str,
        observed_hash: str | None = None,
        size_bytes: int | None = None,
        resource_type: str | None = None,
        source_adapter_id: str | None = None,
        source_identity: str | None = None,
        source_event_id: uuid.UUID | None = None,
        ingress_receipt_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> IndexResult:
        """Work out the Resource/Version writes one observation implies.

        ``observed_hash`` is normally supplied by the adapter that already
        hashed the file; when it is absent the content is read and hashed here.
        """
        resource = self.store.get_resource_by_uri(uri)
        resource_is_new = resource is None
        if resource is None:
            resource = Resource(
                uri=uri,
                resource_type=resource_type or resource_type_for(uri),
                source_adapter_id=source_adapter_id,
                source_identity=source_identity,
                metadata=dict(metadata or {}),
            )

        if observed_hash is None:
            try:
                observed_hash = content_hash(self.read_bytes(locator))
            except (OSError, ScopeViolation) as exc:
                return IndexResult(
                    resource=resource,
                    resource_is_new=resource_is_new,
                    reason=f"content unreadable: {exc}",
                )

        # Content dedup (spec §11): re-observing the same bytes is not a change.
        if not resource_is_new:
            existing = self.store.get_version_by_hash(resource.id, observed_hash)
            if existing is not None:
                return IndexResult(
                    resource=resource,
                    resource_is_new=False,
                    unchanged_version=existing,
                    reason="content_hash unchanged",
                )

        number = 1 if resource_is_new else self.store.next_version_number(resource.id)
        version = ResourceVersion(
            resource_id=resource.id,
            version=number,
            content_hash=observed_hash,
            size_bytes=size_bytes,
            source_event_id=source_event_id,
            ingress_receipt_id=ingress_receipt_id,
            locator=locator,
            metadata=dict(metadata or {}),
        )
        resource.current_version_id = version.id
        return IndexResult(
            resource=resource, version=version, resource_is_new=resource_is_new
        )
