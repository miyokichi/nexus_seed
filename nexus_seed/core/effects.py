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
    extension: "ExtensionEffects" = None
    construction: "ConstructionEffects" = None
    installation: "InstallationEffects" = None
    autonomy: "AutonomyEffects" = None

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
                self.extension is not None and self.extension.any,
                self.construction is not None and self.construction.any,
                self.installation is not None and self.installation.any,
                self.autonomy is not None and self.autonomy.any,
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


@dataclass(frozen=True)
class ExtensionEffects:
    """Phase 5A writes: what we lack, what we proposed, and what was decided.

    Grouped apart from :class:`WorkEffects` because they are about different
    subjects (Invariant 85).  A WorkRequirement is something the world asks of
    us; a CapabilityGap is something we are missing, and writing one must never
    look like editing the other.
    """

    gaps: list = field(default_factory=list)
    gap_updates: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    proposal_updates: list = field(default_factory=list)
    decisions: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.gaps
            or self.gap_updates
            or self.proposals
            or self.proposal_updates
            or self.decisions
        )


@dataclass(frozen=True)
class ConstructionEffects:
    """Phase 5B writes: isolated build intent, progress and evidence."""

    plans: list = field(default_factory=list)
    plan_updates: list = field(default_factory=list)
    step_updates: list = field(default_factory=list)
    workspaces: list = field(default_factory=list)
    workspace_updates: list = field(default_factory=list)
    grants: list = field(default_factory=list)
    grant_updates: list = field(default_factory=list)
    verification_checks: list = field(default_factory=list)
    results: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.plans or self.plan_updates or self.step_updates or self.workspaces
            or self.workspace_updates or self.grants or self.grant_updates
            or self.verification_checks or self.results
        )


@dataclass(frozen=True)
class InstallationEffects:
    """Phase 5C writes: production promotion, evidence and activation."""

    plans: list = field(default_factory=list)
    plan_updates: list = field(default_factory=list)
    step_updates: list = field(default_factory=list)
    grants: list = field(default_factory=list)
    grant_updates: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    results: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    activations: list = field(default_factory=list)
    rollbacks: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.plans or self.plan_updates or self.step_updates or self.grants
            or self.grant_updates or self.checks or self.results or self.decisions
            or self.activations or self.rollbacks
        )


@dataclass(frozen=True)
class AutonomyEffects:
    """Phase 5D writes: coordination, subscribers, decisions, and attempts."""

    sessions: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    attempts: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.sessions or self.subscribers or self.decisions or self.attempts)


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
    result.capability_gap_updates = normalize_status_updates(
        result.capability_gap_updates, label="capability gap"
    )
    result.extension_proposal_updates = _normalize_reasoned_updates(
        result.extension_proposal_updates, label="extension proposal"
    )
    result.construction_plan_updates = normalize_status_updates(
        result.construction_plan_updates, label="construction plan"
    )
    result.construction_step_updates = normalize_status_updates(
        result.construction_step_updates, label="construction step"
    )
    result.sandbox_workspace_updates = _normalize_timed_updates(
        result.sandbox_workspace_updates, label="sandbox workspace"
    )
    result.construction_grant_updates = normalize_status_updates(
        result.construction_grant_updates, label="construction grant"
    )
    result.installation_plan_updates = _normalize_reasoned_updates(
        result.installation_plan_updates, label="installation plan"
    )
    result.installation_step_updates = normalize_status_updates(
        result.installation_step_updates, label="installation step"
    )
    result.installation_grant_updates = _normalize_timed_updates(
        result.installation_grant_updates, label="installation grant"
    )
    result.acquisition_sessions = _normalize_domain_records(
        result.acquisition_sessions,
        label="acquisition session",
        signature=lambda value: (
            getattr(value.status, "value", value.status),
            getattr(value.current_stage, "value", value.current_stage),
            value.blocked_reason,
        ),
    )
    result.acquisition_subscribers = _normalize_domain_records(
        result.acquisition_subscribers,
        label="acquisition subscriber",
        signature=lambda value: value.status,
    )
    result.acquisition_attempts = _normalize_domain_records(
        result.acquisition_attempts,
        label="acquisition attempt",
        signature=lambda value: (value.status, value.plan_id, value.result_id),
    )
    result.process_instance_updates = _normalize_process_instance_updates(
        result.process_instance_updates
    )
    _check_work_match_conflicts(result)


def _normalize_domain_records(records: list, *, label: str, signature) -> list:
    """Deduplicate identical domain writes and refuse contradictory ones."""

    seen: dict[Any, Any] = {}
    normalized: list = []
    for record in records:
        record_id = record.id
        value = signature(record)
        if record_id in seen:
            if seen[record_id] == value:
                continue
            raise EffectConflictError(
                f"conflicting {label} writes for {record_id}: "
                f"{seen[record_id]!r} then {value!r} in one activation"
            )
        seen[record_id] = value
        normalized.append(record)
    return normalized


def _normalize_reasoned_updates(updates: list[tuple], *, label: str) -> list[tuple]:
    """Collapse duplicate ``(id, status, reasons)`` updates, refusing conflicts.

    The same rule as :func:`normalize_status_updates` for the effects that
    carry their reasons with them (spec §131): saying the same thing twice is
    one intention, and saying two different things about one record in one
    activation is an ambiguity the runtime must not resolve by list order.
    """
    seen: dict[Any, tuple] = {}
    normalized: list[tuple] = []
    for record_id, status, reasons in updates:
        if record_id in seen:
            previous_status, previous_reasons = seen[record_id]
            if previous_status == status and previous_reasons == list(reasons or []):
                continue
            raise EffectConflictError(
                f"conflicting {label} updates for {record_id}: "
                f"{previous_status!r} then {status!r} in one activation"
            )
        seen[record_id] = (status, list(reasons or []))
        normalized.append((record_id, status, reasons))
    return normalized


def _normalize_timed_updates(updates: list[tuple], *, label: str) -> list[tuple]:
    seen: dict[Any, tuple] = {}
    normalized: list[tuple] = []
    for record_id, status, timestamp in updates:
        value = (status, timestamp)
        if record_id in seen:
            if seen[record_id] == value:
                continue
            raise EffectConflictError(
                f"conflicting {label} updates for {record_id}: "
                f"{seen[record_id]!r} then {value!r} in one activation"
            )
        seen[record_id] = value
        normalized.append((record_id, status, timestamp))
    return normalized


def _normalize_process_instance_updates(updates: list[tuple]) -> list[tuple]:
    seen: dict[Any, tuple] = {}
    normalized: list[tuple] = []
    for instance_id, status, output in updates:
        value = (status, output)
        if instance_id in seen:
            if seen[instance_id] == value:
                continue
            raise EffectConflictError(
                f"conflicting process instance updates for {instance_id}: "
                f"{seen[instance_id]!r} then {value!r}"
            )
        seen[instance_id] = value
        normalized.append((instance_id, status, output))
    return normalized


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
