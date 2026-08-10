"""Provenance — walk from a current fact back to the raw event that caused it.

The chain, resolved purely from the database (no LLM, no inference):

    Current State
      -> History entry (the version that is current)
        -> StateDelta (why the value changed)
          -> Observation (what was read)
            -> Raw Event (what happened)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.event import Event
    from ..core.state import StateEntry, StateHistoryEntry
    from .observation import Observation
    from .state_delta import StateDelta


@dataclass
class Provenance:
    """The full lineage of a current world-state fact.

    Any link may be ``None`` if a value was written without that provenance
    (e.g. a plain process that set state without proposing a delta).
    """

    current: "StateEntry"
    history_entry: "StateHistoryEntry | None" = None
    state_delta: "StateDelta | None" = None
    observation: "Observation | None" = None
    source_event: "Event | None" = None


def get_state_provenance(
    entity: str,
    attribute: str,
    *,
    state_store,
    state_delta_store,
    observation_store,
    event_store,
) -> Provenance | None:
    """Resolve the provenance chain for ``entity.attribute`` from storage.

    Returns ``None`` if there is no current fact for that entity/attribute.
    """
    current = state_store.get_current(entity, attribute)
    if current is None:
        return None

    history_entry = (
        state_store.get_history_entry(current.history_id)
        if current.history_id
        else None
    )
    state_delta = (
        state_delta_store.get(current.state_delta_id)
        if current.state_delta_id
        else None
    )
    observation = (
        observation_store.get(current.observation_id)
        if current.observation_id
        else None
    )

    source_event_id = None
    if observation is not None and observation.source_event_id is not None:
        source_event_id = observation.source_event_id
    elif state_delta is not None and state_delta.source_event_id is not None:
        source_event_id = state_delta.source_event_id
    elif current.source_event is not None:
        source_event_id = current.source_event
    source_event = event_store.get(source_event_id) if source_event_id else None

    return Provenance(
        current=current,
        history_entry=history_entry,
        state_delta=state_delta,
        observation=observation,
        source_event=source_event,
    )
