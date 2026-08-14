"""Effect grouping, normalization and conflict detection.

A ``ProcessResult`` accumulated twenty-odd effect lists over Phases 2A–4A.  They
are all the same *kind* of thing — writes to be committed in one transaction —
but two problems had grown with the count:

* the flat list said nothing about which effects belonged together;
* two staged writes to the **same record** in one activation were applied in
  list order, so the last one silently won.  That is not a hypothetical: it bit
  Phase 4A, where recording a capability selection quietly overwrote a work
  status set moments earlier in the same handler.

This module addresses both without inventing a generic ``Effect(type, payload)``
(spec §75) and without renaming the fields every existing handler uses
(spec §77).  :class:`ProcessEffects` is a *view* that groups the existing lists;
:func:`check_conflicts` refuses ambiguity before anything is committed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


class EffectConflictError(Exception):
    """Two staged effects in one activation disagree about the same record.

    Raised *before* committing, so the activation fails cleanly rather than
    landing whichever write happened to be last in a list.  A handler that
    genuinely means to supersede its own earlier decision should stage one
    effect, not two.
    """


@dataclass(frozen=True)
class ProcessLifecycleResult:
    """What happens to the *instance* — as opposed to what it wrote.

    A read-only view over the lifecycle fields of a :class:`ProcessResult`
    (spec §72), so the two concerns can be talked about separately even though
    they are stored flat for compatibility.
    """

    status: Any
    output: dict | None = None
    retryable: bool = False
    retry_delay: float | None = None
    join: Any = None


@dataclass(frozen=True)
class ProcessEffects:
    """Everything an activation wrote, grouped by the layer that owns it.

    A read-only view (spec §71/§73).  The underlying lists stay on
    ``ProcessResult`` so every existing handler and test keeps working; this is
    for reading, reasoning and validating about them as a whole.
    """

    events: list = field(default_factory=list)
    state: list = field(default_factory=list)
    continuations_to_create: list = field(default_factory=list)
    continuations_to_delete: list = field(default_factory=list)
    processes: list = field(default_factory=list)
    timers: list = field(default_factory=list)
    semantic: "SemanticEffects" = None
    work: "WorkEffects" = None
    intelligence: "IntelligenceEffects" = None
    actions: "ActionEffects" = None
    resources: "ResourceEffects" = None
    planning: "PlanningEffects" = None
    decision: "DecisionEffects" = None

    @property
    def is_empty(self) -> bool:
        """Whether this activation wrote nothing at all."""
        return not any(
            (
                self.events,
                self.state,
                self.continuations_to_create,
                self.continuations_to_delete,
                self.processes,
                self.timers,
                self.semantic.any,
                self.work.any,
                self.intelligence.any,
                self.actions.any,
                self.resources.any,
                self.planning.any,
                self.decision is not None and self.decision.any,
            )
        )


@dataclass(frozen=True)
class SemanticEffects:
    """Phase 2B writes: what was read, and what it implies changed."""

    observations: list = field(default_factory=list)
    state_deltas: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.observations or self.state_deltas)


@dataclass(frozen=True)
class WorkEffects:
    """Phase 2C/4A writes: needs, their lifecycle, and how they were matched."""

    requirements: list = field(default_factory=list)
    status_updates: list = field(default_factory=list)
    matches: list = field(default_factory=list)
    capability_matches: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.requirements or self.status_updates or self.matches or self.capability_matches
        )


@dataclass(frozen=True)
class IntelligenceEffects:
    """Phase 3B writes: proposals and the calls that produced them."""

    proposals: list = field(default_factory=list)
    proposal_updates: list = field(default_factory=list)
    llm_invocations: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.proposals or self.proposal_updates or self.llm_invocations)


@dataclass(frozen=True)
class ActionEffects:
    """Phase 3C writes: what we intend outside, and what we attempted."""

    proposals: list = field(default_factory=list)
    proposal_updates: list = field(default_factory=list)
    executions: list = field(default_factory=list)
    decisions: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.proposals or self.proposal_updates or self.executions or self.decisions
        )


@dataclass(frozen=True)
class ResourceEffects:
    """Phase 3E writes: documents, their versions and renderings."""

    resources: list = field(default_factory=list)
    versions: list = field(default_factory=list)
    representations: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.resources or self.versions or self.representations)


@dataclass(frozen=True)
class PlanningEffects:
    """Phase 4B writes: composed plans and their progress."""

    plans_to_create: list = field(default_factory=list)
    plan_updates: list = field(default_factory=list)
    node_updates: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.plans_to_create or self.plan_updates or self.node_updates)


@dataclass(frozen=True)
class DecisionEffects:
    """Phase 4C writes: how a plan came to be the chosen one.

    Grouped apart from :class:`PlanningEffects` because they answer different
    questions.  Planning records *what could be done*; decision records *what
    was chosen and why*, and that record is append-only (Invariant 78).
    """

    evaluations: list = field(default_factory=list)
    selection_proposals: list = field(default_factory=list)
    selection_proposal_updates: list = field(default_factory=list)
    selections: list = field(default_factory=list)
    replan_attempts: list = field(default_factory=list)
    replan_counts: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.evaluations
            or self.selection_proposals
            or self.selection_proposal_updates
            or self.selections
            or self.replan_attempts
            or self.replan_counts
        )


# --- normalization / conflict detection ------------------------------------


def normalize_status_updates(
    updates: list[tuple], *, label: str
) -> list[tuple]:
    """Collapse duplicate status transitions, refusing contradictory ones.

    Two identical updates are the same intention stated twice and are safely
    deduplicated (spec §82).  Two *different* statuses for the same record are
    an ambiguity the runtime must not resolve by list order (spec §80–§81).

    Raises:
        EffectConflictError: If one record is given two different statuses.
    """
    seen: dict[Any, Any] = {}
    normalized: list[tuple] = []
    for record_id, status in updates:
        if record_id in seen:
            if seen[record_id] == status:
                continue  # same intention twice
            raise EffectConflictError(
                f"conflicting {label} updates for {record_id}: "
                f"{seen[record_id]!r} then {status!r} in one activation"
            )
        seen[record_id] = status
        normalized.append((record_id, status))
    return normalized


def check_conflicts(result) -> None:
    """Validate that a result's staged effects do not contradict each other.

    Called before the commit transaction opens, so a conflict fails the
    activation without leaving a partial write.
    """
    result.work_requirement_updates = normalize_status_updates(
        result.work_requirement_updates, label="work requirement"
    )
    result.proposal_updates = normalize_status_updates(
        result.proposal_updates, label="interpretation proposal"
    )
    result.action_proposal_updates = normalize_status_updates(
        result.action_proposal_updates, label="action proposal"
    )
    result.plan_updates = normalize_status_updates(
        result.plan_updates, label="plan"
    )
    result.plan_node_updates = _normalize_node_updates(result.plan_node_updates)
    _check_work_match_conflicts(result)


def _normalize_node_updates(updates: list[tuple]) -> list[tuple]:
    """Collapse duplicate plan-node updates, refusing contradictory statuses."""
    seen: dict[uuid.UUID, tuple] = {}
    normalized: list[tuple] = []
    for node_id, status, instance_id in updates:
        if node_id in seen:
            previous_status, previous_instance = seen[node_id]
            if previous_status == status and previous_instance == instance_id:
                continue
            raise EffectConflictError(
                f"conflicting plan node updates for {node_id}: "
                f"{previous_status!r} then {status!r} in one activation"
            )
        seen[node_id] = (status, instance_id)
        normalized.append((node_id, status, instance_id))
    return normalized


def _check_work_match_conflicts(result) -> None:
    """Refuse a match record that contradicts a status update in the same batch.

    The exact shape of the Phase 4A bug: one effect setting a work status and
    another quietly rewriting it.  Recording a *selection* alongside a status
    update is fine — those are different facts — but two different statuses
    are not.
    """
    statuses = {record_id: status for record_id, status in result.work_requirement_updates}
    for record_id, status, _missing, _selected in result.work_matches:
        if status is None:
            continue  # selection only; the lifecycle is somebody else's business
        if record_id in statuses and statuses[record_id] != status:
            raise EffectConflictError(
                f"conflicting work requirement updates for {record_id}: "
                f"{statuses[record_id]!r} and {status!r} in one activation"
            )
