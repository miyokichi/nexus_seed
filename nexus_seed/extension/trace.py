"""Extension trace — why does this system want to change itself?

The chain this answers (spec §56) is the longest one in NEXUS SEED, and every
link of it already existed::

    ExtensionProposal -> CapabilityGap -> WorkRequirement -> CapabilityMatch
                      -> StateDelta -> Observation -> raw Event

plus, on the other side, how the proposal came to say what it says::

    ExtensionProposal -> AcquisitionCandidates -> LLMInvocation -> ContextSnapshot
                      -> policy decision -> extension_reviewed -> human

Two questions make this worth assembling rather than leaving to a reader with a
SQL client.  *Why does it think it needs this?* is answered by walking left, to
the external event that started everything.  *What did it believe it could do at
the time?* is answered by the ContextSnapshot — the record of the system's
self-model at the moment it decided it was insufficient (spec §54).

Read-only, and from storage alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..capabilities.models import CapabilityWorkMatch
    from ..context.models import ContextSnapshot
    from ..core.event import Event
    from ..work.work_requirement import WorkRequirement
    from .models import CapabilityGap, ExtensionDecisionRecord, ExtensionProposal


@dataclass
class CapabilityGapTrace:
    """One deficiency, and everything said about it."""

    gap: "CapabilityGap"
    work_requirement: "WorkRequirement | None" = None
    matches: list["CapabilityWorkMatch"] = field(default_factory=list)
    proposals: list["ExtensionProposal"] = field(default_factory=list)

    @property
    def missing_capabilities(self) -> list[str]:
        return self.gap.missing_names

    @property
    def source_match(self) -> "CapabilityWorkMatch | None":
        """The matching attempt that concluded this gap (spec §95)."""
        for match in self.matches:
            if match.id == self.gap.source_match_id:
                return match
        return self.matches[-1] if self.matches else None

    @property
    def latest_proposal(self) -> "ExtensionProposal | None":
        return self.proposals[-1] if self.proposals else None

    @property
    def resolved(self) -> bool:
        return self.gap.status.value == "RESOLVED"


@dataclass
class ExtensionTrace:
    """One proposal to extend the system, walked back to the world.

    ``capability_acquired`` is deliberately part of the trace and deliberately
    always ``False`` in Phase 5A (Invariant 89): the distance between "approved"
    and "acquired" is the thing this phase most needs to keep visible.
    """

    proposal: "ExtensionProposal"
    gap: "CapabilityGap | None" = None
    work_requirement: "WorkRequirement | None" = None
    capability_match: "CapabilityWorkMatch | None" = None
    state_delta: object | None = None
    observation: object | None = None
    source_event: "Event | None" = None
    decisions: list["ExtensionDecisionRecord"] = field(default_factory=list)
    review_events: list["Event"] = field(default_factory=list)
    lineage: list["ExtensionProposal"] = field(default_factory=list)
    llm_invocation: object | None = None
    context_snapshot: "ContextSnapshot | None" = None

    @property
    def candidates(self) -> list[dict]:
        """The acquisition routes considered when this proposal was written."""
        return list((self.proposal.analysis or {}).get("candidates") or [])

    @property
    def candidate_strategies(self) -> list[str]:
        """The strategies the analyzer allowed — the boundary of the proposal."""
        return list(self.proposal.candidate_strategies)

    @property
    def final_decision(self) -> "ExtensionDecisionRecord | None":
        return self.decisions[-1] if self.decisions else None

    @property
    def human_reviewed(self) -> bool:
        """Whether a person actually decided this (Invariant 91)."""
        return any(d.reviewed_by_event_id is not None for d in self.decisions)

    @property
    def approved(self) -> bool:
        return self.proposal.status.value == "APPROVED"

    @property
    def capability_acquired(self) -> bool:
        """Always ``False`` in Phase 5A — approval is not acquisition (§62)."""
        return False


def get_capability_gap_trace(
    gap_id,
    *,
    extension_store,
    work_requirement_store,
    capability_store,
) -> CapabilityGapTrace | None:
    """Resolve one gap, its need and everything proposed about it."""
    gap = extension_store.get_gap(gap_id)
    if gap is None:
        return None
    return CapabilityGapTrace(
        gap=gap,
        work_requirement=work_requirement_store.get(gap.work_requirement_id),
        matches=capability_store.matches_for(gap.work_requirement_id),
        proposals=extension_store.proposals_for_gap(gap.id),
    )


def get_extension_trace(
    proposal_id,
    *,
    extension_store,
    work_requirement_store,
    capability_store,
    state_delta_store,
    observation_store,
    event_store,
    llm_invocation_store=None,
    context_snapshot_store=None,
) -> ExtensionTrace | None:
    """Walk a proposal back to the raw event, and forward to its decisions."""
    proposal = extension_store.get_proposal(proposal_id)
    if proposal is None:
        return None

    trace = ExtensionTrace(
        proposal=proposal,
        gap=extension_store.get_gap(proposal.capability_gap_id),
        decisions=extension_store.decisions_for_proposal(proposal.id),
        lineage=extension_store.proposals_in_chain(
            proposal.root_proposal_id or proposal.id
        ),
    )

    if trace.gap is not None:
        trace.work_requirement = work_requirement_store.get(trace.gap.work_requirement_id)
        for match in capability_store.matches_for(trace.gap.work_requirement_id):
            if match.id == trace.gap.source_match_id:
                trace.capability_match = match
                break
            trace.capability_match = trace.capability_match or match

    requirement = trace.work_requirement
    if requirement is not None:
        if requirement.source_state_delta_id is not None:
            trace.state_delta = state_delta_store.get(requirement.source_state_delta_id)
        if trace.state_delta is not None and trace.state_delta.observation_id:
            trace.observation = observation_store.get(trace.state_delta.observation_id)
        source_event_id = (
            getattr(trace.observation, "source_event_id", None)
            or getattr(trace.state_delta, "source_event_id", None)
            or requirement.source_event_id
        )
        if source_event_id is not None:
            trace.source_event = event_store.get(source_event_id)

    # The reviews that touched this chain, found by the ids the decisions kept.
    reviewed_ids = [d.reviewed_by_event_id for d in trace.decisions if d.reviewed_by_event_id]
    for event_id in reviewed_ids:
        event = event_store.get(event_id)
        if event is not None:
            trace.review_events.append(event)

    if llm_invocation_store is not None and proposal.llm_invocation_id is not None:
        trace.llm_invocation = llm_invocation_store.get(proposal.llm_invocation_id)
    if context_snapshot_store is not None and proposal.context_snapshot_id is not None:
        trace.context_snapshot = context_snapshot_store.get(proposal.context_snapshot_id)
    return trace


__all__ = [
    "CapabilityGapTrace",
    "ExtensionTrace",
    "get_capability_gap_trace",
    "get_extension_trace",
]
