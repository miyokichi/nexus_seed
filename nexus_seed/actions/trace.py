"""Action trace — why did this system touch the outside world?

Walks the outbound chain back to the event that started everything::

    ActionExecution
      -> ActionProposal
        -> ProcessInstance
          -> WorkRequirement
            -> StateDelta
              -> Observation
                -> Raw Event

plus the ContextSnapshot the decision was made against (spec §47), the
authorization decisions with their permission provenance (spec §49), and any
``action_reviewed`` events a human contributed (spec §48).

Resolved purely from the database — no inference, no re-execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context.models import ContextSnapshot
    from ..core.event import Event
    from ..core.process import ProcessInstance
    from ..work.work_requirement import WorkRequirement
    from ..world.observation import Observation
    from ..world.state_delta import StateDelta
    from .models import ActionDecisionRecord, ActionExecution, ActionProposal


@dataclass
class ActionTrace:
    """The full lineage of one action, from execution back to raw event."""

    proposal: "ActionProposal"
    executions: list["ActionExecution"] = field(default_factory=list)
    decisions: list["ActionDecisionRecord"] = field(default_factory=list)
    process_instance: "ProcessInstance | None" = None
    work_requirement: "WorkRequirement | None" = None
    state_delta: "StateDelta | None" = None
    observation: "Observation | None" = None
    source_event: "Event | None" = None
    trigger_event: "Event | None" = None
    context_snapshot: "ContextSnapshot | None" = None
    review_events: list["Event"] = field(default_factory=list)

    @property
    def succeeded_execution(self) -> "ActionExecution | None":
        """The attempt that actually produced the side effect, if any."""
        for execution in self.executions:
            if execution.status.value == "SUCCEEDED":
                return execution
        return None


def get_action_trace(
    action_proposal_id,
    *,
    action_proposal_store,
    action_execution_store,
    action_decision_store,
    process_store,
    work_requirement_store,
    state_delta_store,
    observation_store,
    event_store,
    context_snapshot_store=None,
) -> ActionTrace | None:
    """Resolve the full trace for an action proposal from storage.

    Returns ``None`` if the proposal does not exist.
    """
    proposal = action_proposal_store.get(action_proposal_id)
    if proposal is None:
        return None

    process_instance = (
        process_store.get_instance(proposal.created_by_process_id)
        if proposal.created_by_process_id
        else None
    )

    requirement = None
    if proposal.source_work_requirement_id is not None:
        requirement = work_requirement_store.get(proposal.source_work_requirement_id)
    elif process_instance is not None and process_instance.work_requirement_id:
        requirement = work_requirement_store.get(process_instance.work_requirement_id)

    state_delta = (
        state_delta_store.get(requirement.source_state_delta_id)
        if requirement is not None and requirement.source_state_delta_id
        else None
    )
    observation = (
        observation_store.get(state_delta.observation_id)
        if state_delta is not None and state_delta.observation_id
        else None
    )

    source_event_id = None
    for candidate in (
        observation.source_event_id if observation is not None else None,
        state_delta.source_event_id if state_delta is not None else None,
        requirement.source_event_id if requirement is not None else None,
        proposal.trigger_event_id,
    ):
        if candidate is not None:
            source_event_id = candidate
            break

    context_snapshot = (
        context_snapshot_store.get(proposal.context_snapshot_id)
        if context_snapshot_store is not None and proposal.context_snapshot_id
        else None
    )

    # Human review events name the proposal they settled; a modify-chain shares
    # a root id, so accept either.
    chain_ids = {str(proposal.id), str(proposal.root_proposal_id)}
    review_events = [
        event
        for event in event_store.by_type("action_reviewed")
        if str(event.payload.get("proposal_id")) in chain_ids
    ]

    return ActionTrace(
        proposal=proposal,
        executions=action_execution_store.for_proposal(proposal.id),
        decisions=action_decision_store.for_proposal(proposal.id),
        process_instance=process_instance,
        work_requirement=requirement,
        state_delta=state_delta,
        observation=observation,
        source_event=event_store.get(source_event_id) if source_event_id else None,
        trigger_event=(
            event_store.get(proposal.trigger_event_id)
            if proposal.trigger_event_id
            else None
        ),
        context_snapshot=context_snapshot,
        review_events=review_events,
    )
