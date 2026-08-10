"""Context — the working information for one process activation.

Context is intentionally separate from State:

* **State** is long-lived knowledge about the world.
* **Context** is the transient working set a process needs *right now* — a
  snapshot of the relevant state plus the relevant events.

Context is not a conversation history.  Phase 1 ships a deliberately simple
:func:`build_context`; richer context assembly is a later phase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .event import Event

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..storage.event_store import EventStore
    from ..storage.state_store import StateStore
    from .process import ProcessInstance


@dataclass
class Context:
    """Working information assembled for a single process activation.

    Attributes:
        relevant_state: A snapshot of world state the process may need.
        relevant_events: Events considered relevant to this activation.
        notes: Free-form scratch space for the context builder.
    """

    relevant_state: dict[str, Any] = field(default_factory=dict)
    relevant_events: list[Event] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


def build_context(
    instance: "ProcessInstance",
    state_store: "StateStore",
    event_store: "EventStore",
    *,
    correlation_id: uuid.UUID | None = None,
) -> Context:
    """Assemble a :class:`Context` for a process activation.

    Phase 1 policy: include the full world-state snapshot and every event that
    shares the activation's ``correlation_id``.  This is simple on purpose; the
    boundary exists so smarter selection can be added later without touching
    process handlers.
    """

    relevant_state = state_store.snapshot()
    relevant_events: list[Event] = []
    if correlation_id is not None:
        relevant_events = event_store.by_correlation(correlation_id)
    return Context(relevant_state=relevant_state, relevant_events=relevant_events)
