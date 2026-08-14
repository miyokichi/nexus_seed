"""Durable event delivery — persisting an event is a promise to consider it.

Runtime/infrastructure, not a core primitive and not domain data::

    Event persisted  ->  EventDelivery (PENDING)
                     ->  DurableEventDispatcher -> Router -> activations
                     ->  EventDelivery (DELIVERED), same transaction
"""

from .dispatcher import DEFAULT_BATCH, DurableEventDispatcher, dispatchable_at
from .models import (
    OUTSTANDING,
    EventDelivery,
    EventDeliveryStatus,
    backoff_seconds,
)

__all__ = [
    "DEFAULT_BATCH",
    "DurableEventDispatcher",
    "EventDelivery",
    "EventDeliveryStatus",
    "OUTSTANDING",
    "backoff_seconds",
    "dispatchable_at",
]
