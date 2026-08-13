"""Ingress trace — what in the outside world caused all of this?

Phase 3C could already answer "why did we act?" back to a raw Event.  Phase 3D
extends the same chain one hop further out, to the external occurrence itself::

    External identity (adapter_id, source_event_key)
      -> IngressReceipt
        -> Event
          -> Observation
            -> StateDelta
              -> World State
                -> WorkRequirement
                  -> ActionProposal -> ActionExecution

This is not a new trace system (spec §42).  It walks the *existing* links and
hands off to :func:`~nexus_seed.actions.trace.get_action_trace` for the
outbound half, so there is one chain, resolved from the database only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..actions.models import ActionProposal
    from ..core.event import Event
    from ..work.work_requirement import WorkRequirement
    from ..world.observation import Observation
    from ..world.state_delta import StateDelta
    from .models import IngressReceipt


@dataclass
class IngressTrace:
    """The lineage of one external occurrence through the whole system."""

    receipt: "IngressReceipt"
    event: "Event | None" = None
    observations: list["Observation"] = field(default_factory=list)
    state_deltas: list["StateDelta"] = field(default_factory=list)
    work_requirements: list["WorkRequirement"] = field(default_factory=list)
    action_proposals: list["ActionProposal"] = field(default_factory=list)

    @property
    def source_identity(self) -> tuple[str, str]:
        """The ``(adapter_id, source_event_key)`` this all started from."""
        return (self.receipt.adapter_id, self.receipt.source_event_key)

    @property
    def reached_the_world(self) -> bool:
        """Whether this external input ended in an action on the outside."""
        return any(
            p.status.value == "SUCCEEDED" for p in self.action_proposals
        )


def get_ingress_trace(
    event_id,
    *,
    ingress_receipt_store,
    event_store,
    observation_store,
    state_delta_store,
    work_requirement_store,
    action_proposal_store=None,
) -> IngressTrace | None:
    """Resolve everything one ingested Event led to.

    Returns ``None`` if the event has no receipt — i.e. it originated inside
    NEXUS SEED rather than arriving from the world.
    """
    receipt = ingress_receipt_store.for_event(event_id)
    if receipt is None:
        return None
    return _trace_from_receipt(
        receipt,
        event_store=event_store,
        observation_store=observation_store,
        state_delta_store=state_delta_store,
        work_requirement_store=work_requirement_store,
        action_proposal_store=action_proposal_store,
    )


def get_ingress_trace_by_source_key(
    adapter_id: str,
    source_event_key: str,
    *,
    ingress_receipt_store,
    event_store,
    observation_store,
    state_delta_store,
    work_requirement_store,
    action_proposal_store=None,
) -> IngressTrace | None:
    """Resolve the trace starting from the *external* identity (spec §41).

    The question an operator actually asks — "what did delivery ``abc-123`` from
    the payroll webhook end up doing?" — is answerable without knowing any
    NEXUS SEED id at all.
    """
    receipt = ingress_receipt_store.get_by_source_key(adapter_id, source_event_key)
    if receipt is None:
        return None
    return _trace_from_receipt(
        receipt,
        event_store=event_store,
        observation_store=observation_store,
        state_delta_store=state_delta_store,
        work_requirement_store=work_requirement_store,
        action_proposal_store=action_proposal_store,
    )


def _trace_from_receipt(
    receipt,
    *,
    event_store,
    observation_store,
    state_delta_store,
    work_requirement_store,
    action_proposal_store,
) -> IngressTrace:
    event = event_store.get(receipt.event_id) if receipt.event_id else None
    if event is None:
        return IngressTrace(receipt=receipt)

    observations = observation_store.for_event(event.id)
    observation_ids = {o.id for o in observations}

    # A delta descends from this event either directly or through one of its
    # observations; an LLM-driven reading uses the latter.
    state_deltas = [
        delta
        for delta in state_delta_store.all()
        if delta.source_event_id == event.id or delta.observation_id in observation_ids
    ]
    delta_ids = {d.id for d in state_deltas}

    work_requirements = [
        requirement
        for requirement in work_requirement_store.all()
        if requirement.source_state_delta_id in delta_ids
        or requirement.source_event_id == event.id
    ]
    requirement_ids = {r.id for r in work_requirements}

    action_proposals = []
    if action_proposal_store is not None:
        action_proposals = [
            proposal
            for proposal in action_proposal_store.all()
            if proposal.source_work_requirement_id in requirement_ids
        ]

    return IngressTrace(
        receipt=receipt,
        event=event,
        observations=observations,
        state_deltas=state_deltas,
        work_requirements=work_requirements,
        action_proposals=action_proposals,
    )
