"""Resource / Representation traces — which bytes, from where, read by whom.

Extends the existing chain outward one more hop.  Phase 3D could say an Event
came from a delivery; Phase 3E can say which *version of which document* a
decision was made against, and which extractor produced the rendering that was
actually read::

    Representation
      -> Extraction Process
        -> ResourceVersion
          -> Resource
            -> Source Event
              -> IngressReceipt
                -> External source

No new trace system (spec §50): these walk existing links and join up with the
ingress and action traces already in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.event import Event
    from ..core.process import ProcessInstance
    from ..ingress.models import IngressReceipt
    from .models import Resource, ResourceRepresentation, ResourceVersion


@dataclass
class ResourceTrace:
    """Where one version of a Resource came from."""

    version: "ResourceVersion"
    resource: "Resource | None" = None
    source_event: "Event | None" = None
    ingress_receipt: "IngressReceipt | None" = None
    representations: list["ResourceRepresentation"] = field(default_factory=list)

    @property
    def external_identity(self) -> tuple[str, str] | None:
        """``(adapter_id, source_event_key)`` this version entered through."""
        if self.ingress_receipt is None:
            return None
        return (self.ingress_receipt.adapter_id, self.ingress_receipt.source_event_key)


@dataclass
class RepresentationTrace:
    """How one rendering was produced, and of what."""

    representation: "ResourceRepresentation"
    version: "ResourceVersion | None" = None
    resource: "Resource | None" = None
    extracted_by: "ProcessInstance | None" = None
    source_event: "Event | None" = None
    ingress_receipt: "IngressReceipt | None" = None

    @property
    def extractor(self) -> tuple[str, str]:
        """``(extractor_name, extractor_version)`` that produced it."""
        return (
            self.representation.extractor_name,
            self.representation.extractor_version,
        )


def get_resource_trace(
    resource_version_id,
    *,
    resource_store,
    event_store,
    ingress_receipt_store=None,
) -> ResourceTrace | None:
    """Trace a ResourceVersion back to the external source that produced it."""
    version = resource_store.get_version(resource_version_id)
    if version is None:
        return None
    return ResourceTrace(
        version=version,
        resource=resource_store.get_resource(version.resource_id),
        source_event=(
            event_store.get(version.source_event_id) if version.source_event_id else None
        ),
        ingress_receipt=_receipt_for(version, ingress_receipt_store),
        representations=resource_store.list_representations(version.id),
    )


def get_representation_trace(
    representation_id,
    *,
    resource_store,
    process_store,
    event_store,
    ingress_receipt_store=None,
) -> RepresentationTrace | None:
    """Trace a Representation back through its extractor to the outside world."""
    representation = resource_store.get_representation(representation_id)
    if representation is None:
        return None

    version = resource_store.get_version(representation.resource_version_id)
    resource = resource_store.get_resource(version.resource_id) if version else None
    extracted_by = (
        process_store.get_instance(representation.created_by_process_id)
        if representation.created_by_process_id
        else None
    )
    source_event = (
        event_store.get(version.source_event_id)
        if version is not None and version.source_event_id
        else None
    )
    return RepresentationTrace(
        representation=representation,
        version=version,
        resource=resource,
        extracted_by=extracted_by,
        source_event=source_event,
        ingress_receipt=_receipt_for(version, ingress_receipt_store),
    )


def _receipt_for(version, ingress_receipt_store):
    """Resolve a version's ingress receipt, by id or via its source event."""
    if version is None or ingress_receipt_store is None:
        return None
    if version.ingress_receipt_id is not None:
        return ingress_receipt_store.get(version.ingress_receipt_id)
    if version.source_event_id is not None:
        return ingress_receipt_store.for_event(version.source_event_id)
    return None
