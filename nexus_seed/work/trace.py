"""Work trace — why is this process running? Trace it back to the raw event.

    ProcessInstance
      -> WorkRequirement
        -> StateDelta
          -> Observation
            -> Raw Event

Resolved purely from the database (no inference).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.event import Event
    from ..core.process import ProcessInstance
    from ..world.observation import Observation
    from ..world.state_delta import StateDelta
    from .work_requirement import WorkRequirement


@dataclass
class WorkTrace:
    """The lineage of a unit of work, from process back to raw event."""

    requirement: "WorkRequirement"
    process_instance: "ProcessInstance | None" = None
    state_delta: "StateDelta | None" = None
    observation: "Observation | None" = None
    source_event: "Event | None" = None


def get_work_trace(
    work_requirement_id,
    *,
    work_requirement_store,
    process_store,
    state_delta_store,
    observation_store,
    event_store,
) -> WorkTrace | None:
    """Resolve the full trace for a work requirement from storage.

    Returns ``None`` if the requirement does not exist.
    """
    requirement = work_requirement_store.get(work_requirement_id)
    if requirement is None:
        return None

    processes = process_store.find_by_work_requirement_id(requirement.id)
    process_instance = processes[0] if processes else None

    state_delta = (
        state_delta_store.get(requirement.source_state_delta_id)
        if requirement.source_state_delta_id
        else None
    )
    observation = (
        observation_store.get(state_delta.observation_id)
        if state_delta is not None and state_delta.observation_id
        else None
    )

    source_event_id = None
    if observation is not None and observation.source_event_id is not None:
        source_event_id = observation.source_event_id
    elif state_delta is not None and state_delta.source_event_id is not None:
        source_event_id = state_delta.source_event_id
    elif requirement.source_event_id is not None:
        source_event_id = requirement.source_event_id
    source_event = event_store.get(source_event_id) if source_event_id else None

    return WorkTrace(
        requirement=requirement,
        process_instance=process_instance,
        state_delta=state_delta,
        observation=observation,
        source_event=source_event,
    )
