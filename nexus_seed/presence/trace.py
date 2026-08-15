"""Experience reconstruction from existing Event, State and action journals."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..core.event import Event
from ..core.state import StateEntry
from .models import ExperienceRecord


@dataclass(frozen=True, slots=True)
class ExperienceTrace:
    """Joined view of an experience recipe, its source and reflected lesson."""

    experience_event: Event
    record: ExperienceRecord
    source_event: Event | None
    reflection: StateEntry | None
    action_trace: object | None = None
    work_trace: object | None = None


def get_experience_trace(runtime, experience_event_id: uuid.UUID | str) -> ExperienceTrace | None:
    """Reconstruct an Experience using only existing durable stores and traces."""

    try:
        event_id = uuid.UUID(str(experience_event_id))
    except (TypeError, ValueError):
        return None
    event = runtime.event_store.get(event_id)
    if event is None or event.type != "experience_recorded":
        return None
    try:
        record = ExperienceRecord.from_dict(event.payload)
    except (KeyError, TypeError, ValueError):
        return None
    source = runtime.event_store.get(record.source_event_id)
    reflection = runtime.state_store.get_current(f"experience:{event.id}", "reflection")
    proposal_id = record.action.get("action_proposal_id")
    work_id = record.intention.get("work_requirement_id")
    action_trace = None
    work_trace = None
    if proposal_id:
        proposal = runtime.action_proposal_store.get(uuid.UUID(str(proposal_id)))
        if proposal is not None:
            action_trace = runtime.get_action_trace(proposal.id)
    if work_id:
        work_trace = runtime.get_work_trace(uuid.UUID(str(work_id)))
    return ExperienceTrace(event, record, source, reflection, action_trace, work_trace)


__all__ = ["ExperienceTrace", "get_experience_trace"]
