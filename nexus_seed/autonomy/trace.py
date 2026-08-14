"""Joined audit trace for a Phase 5D capability acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AcquisitionTrace:
    """Session plus every existing boundary record it coordinated."""

    session: Any
    subscribers: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    capability_gap: Any = None
    source_work_requirement: Any = None
    extension_proposal: Any = None
    construction_plan: Any = None
    construction_result: Any = None
    installation_plan: Any = None
    installation_trace: Any = None
    events: list = field(default_factory=list)


def get_acquisition_trace(
    session_id, *, autonomy_store, extension_store, construction_store,
    installation_store, work_requirement_store, event_store,
    installation_trace_getter,
) -> AcquisitionTrace | None:
    """Join policy decisions to 5A/5B/5C evidence and reconciliation events."""

    session = autonomy_store.get_session(session_id)
    if session is None:
        return None
    installation_trace = (
        installation_trace_getter(session.installation_plan_id)
        if session.installation_plan_id else None
    )
    return AcquisitionTrace(
        session=session,
        subscribers=autonomy_store.subscribers(session.id),
        decisions=autonomy_store.decisions(session.id),
        attempts=autonomy_store.attempts(session.id),
        capability_gap=extension_store.get_gap(session.capability_gap_id),
        source_work_requirement=work_requirement_store.get(session.source_work_requirement_id),
        extension_proposal=(
            extension_store.get_proposal(session.extension_proposal_id)
            if session.extension_proposal_id else None
        ),
        construction_plan=(
            construction_store.get_plan(session.construction_plan_id)
            if session.construction_plan_id else None
        ),
        construction_result=(
            construction_store.get_result(session.construction_result_id)
            if session.construction_result_id else None
        ),
        installation_plan=(
            installation_store.get_plan(session.installation_plan_id)
            if session.installation_plan_id else None
        ),
        installation_trace=installation_trace,
        events=event_store.referencing_entity(str(session.id)),
    )


__all__ = ["AcquisitionTrace", "get_acquisition_trace"]
