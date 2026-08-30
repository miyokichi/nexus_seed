"""Trace an external source identity into the durable Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from nexus_observer._support.core.event import Event
    from ....core.process import ProcessInstance
    from .models import IngressReceipt


@dataclass
class IngressTrace:
    """Receipt, Event and Process activations caused by one outside occurrence."""

    receipt: "IngressReceipt"
    event: "Event | None" = None
    process_instances: list["ProcessInstance"] = field(default_factory=list)

    @property
    def source_identity(self) -> tuple[str, str]:
        return self.receipt.adapter_id, self.receipt.source_event_key


def get_ingress_trace(
    event_id, *, ingress_receipt_store, event_store, process_store
) -> IngressTrace | None:
    """Resolve the current trace for one externally ingested Event."""
    receipt = ingress_receipt_store.for_event(event_id)
    return (
        _trace(receipt, event_store=event_store, process_store=process_store)
        if receipt is not None
        else None
    )


def get_ingress_trace_by_source_key(
    adapter_id: str,
    source_event_key: str,
    *,
    ingress_receipt_store,
    event_store,
    process_store,
) -> IngressTrace | None:
    """Resolve a trace from the external adapter identity."""
    receipt = ingress_receipt_store.get_by_source_key(adapter_id, source_event_key)
    return (
        _trace(receipt, event_store=event_store, process_store=process_store)
        if receipt is not None
        else None
    )


def _trace(receipt, *, event_store, process_store) -> IngressTrace:
    event = event_store.get(receipt.event_id) if receipt.event_id else None
    instances = []
    if event is not None:
        instances = [
            instance
            for instance in process_store.all_instances()
            if instance.trigger_event_id == event.id
        ]
    return IngressTrace(receipt=receipt, event=event, process_instances=instances)
