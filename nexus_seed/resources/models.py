"""Resource domain models — the things out there, and what we made of them.

Phase 3D could say *that* a file changed.  Phase 3E can say *what it is*, keep
its history, and hand a process a usable rendering of it.  Three levels, kept
strictly apart:

    Resource                what the thing IS          design_review.pptx
      ResourceVersion       what it CONTAINED at a time  v1 (hash AAA), v2 (BBB)
        ResourceRepresentation  what we MADE of that content  text / structure

Collapsing any two of these breaks something real.  If a Resource were keyed by
content hash, editing a file would produce a *different resource* and its
history would disappear.  If a Representation lived on the Resource rather than
the Version, "what did the AI read?" could never be answered after the file
changed.

None of these are core primitives (the six stay fixed); they are domain data
under ``resources/``, like ``world/``, ``work/``, ``actions/`` and ``ingress/``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.event import utcnow


def content_hash(data: bytes) -> str:
    """Return ``sha256-<hex>`` for raw bytes."""
    return f"sha256-{hashlib.sha256(data).hexdigest()}"


def text_hash(text: str) -> str:
    """Return the content hash of ``text`` encoded as UTF-8."""
    return content_hash(text.encode("utf-8"))


@dataclass
class Resource:
    """A referenceable thing in the outside world, across all its versions.

    Attributes:
        uri: Stable identity of the thing itself (``file:///project/spec.pdf``).
            Unique — this is what makes "the same file, edited" one Resource
            with two versions rather than two unrelated Resources.
        resource_type: A coarse kind (``"text"``, ``"csv"``, ``"json"``…),
            used to pick extractors.  Not a MIME registry.
        source_adapter_id / source_identity: which adapter observed it and under
            what external identity, so a Resource traces back to the world.
        current_version_id: Pointer to the newest version, kept for cheap reads.
            A projection of ``resource_versions``, never the source of truth.
        metadata: Free-form extras.
    """

    uri: str
    resource_type: str = "unknown"
    source_adapter_id: str | None = None
    source_identity: str | None = None
    current_version_id: uuid.UUID | None = None
    metadata: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class ResourceVersion:
    """What one Resource contained at one point in time — immutable.

    A change never updates a version; it appends version N+1 (spec §7).  That is
    what lets a ContextSnapshot keep pointing at the exact bytes a decision was
    made against, however much the file moves on afterwards.

    Attributes:
        version: Monotonically increasing within the Resource, starting at 1.
        content_hash: Identity of the content.  Two observations with the same
            hash are the same version, not two.
        locator: Where the content can be read from now (a path, a URL).  Unlike
            ``content_hash`` this may go stale — the world can delete a file.
        source_event_id / ingress_receipt_id: the event and ingress receipt this
            version was learned from (Invariant 39 provenance).
    """

    resource_id: uuid.UUID
    version: int = 1
    content_hash: str | None = None
    size_bytes: int | None = None
    source_event_id: uuid.UUID | None = None
    ingress_receipt_id: uuid.UUID | None = None
    locator: str = ""
    metadata: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ResourceRepresentation:
    """A rendering of one ResourceVersion, produced by one extractor.

    Attached to the *Version*, never to the Resource: a representation is only
    true of the bytes it was extracted from.

    Its logical identity is
    ``(resource_version_id, representation_type, extractor_name,
    extractor_version)`` (spec §14).  The extractor version is part of it on
    purpose — improving an extractor should produce a *new* representation
    alongside the old one, not silently rewrite what a past decision was based
    on.

    Attributes:
        representation_type: ``"text"``, ``"structure"``, ``"metadata"``…
        content: The rendering itself (JSON-serialisable).
        created_by_process_id: The extraction Process that produced it —
            extraction is a Process, never a Runtime feature (Invariant 38).
    """

    resource_version_id: uuid.UUID
    representation_type: str
    content: Any = None
    extractor_name: str = ""
    extractor_version: str = "1"
    metadata: dict = field(default_factory=dict)
    created_by_process_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """The four-part logical identity used for deduplication."""
        return (
            str(self.resource_version_id),
            self.representation_type,
            self.extractor_name,
            self.extractor_version,
        )
