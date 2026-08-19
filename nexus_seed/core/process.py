"""Process — the single unifying primitive of NEXUS SEED.

A **Process** is *anything* the system does.  Skill, Agent, Workflow, Harness,
Deep Research, a resident monitor — none of these are separate base types.  They
are all Processes; those words describe the *role* a process plays in a given
context, not a different data model (a role may be recorded in
``ProcessDefinition.metadata``, never as a new core type).

Two things are kept apart:

* :class:`ProcessDefinition` — *what* a process does (static, stateless).
* :class:`ProcessInstance` — a *running* process (has state, status, identity).

Every process handler shares the same execution interface::

    async def handler(ctx: ProcessContext) -> ProcessResult: ...

Handlers do not touch storage.  They stage state writes through ``ctx.state``
and return every effect in a :class:`ProcessResult`; the runtime commits the
whole thing in one transaction (Phase 2A: atomic process transition).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from .continuation import Continuation
from .context import Context
from .event import Event, utcnow
from .state import StateChange, StateView
from ..world.observation import Observation
from ..world.state_delta import StateDelta
from ..work.work_requirement import WorkRequirement, WorkStatus
from ..context.requirements import ContextRequirements

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context.models import ProcessContextView
    from ..backends.base import LLMInvocation
    from ..intelligence.proposal import InterpretationProposal
    from ..actions.models import (
        ActionDecisionRecord,
        ActionExecution,
        ActionProposal,
    )
    from ..resources.models import (
        Resource,
        ResourceRepresentation,
        ResourceVersion,
    )


class ProcessStatus(str, Enum):
    """Lifecycle status of a :class:`ProcessInstance`."""

    RUNNABLE = "RUNNABLE"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    #: Phase 5G operator pause.  Resume returns to RUNNABLE and recompiles Context.
    PAUSED = "PAUSED"


class RetryableError(Exception):
    """Raise from a handler to signal a *transient* failure worth retrying."""


@dataclass(frozen=True)
class ProcessDefinition:
    """The static definition of a process.

    Attributes:
        name: Logical name (e.g. ``"resistance_analysis"``).
        version: Definition version string.
        handler: Name of the registered handler callable to run.
        trigger_event_types: Event types that start a fresh instance.
        max_retries: How many times a retryable failure may be retried.
        metadata: Free-form tags (e.g. ``{"role": "skill"}``).  Never a new
            core type — just annotations on a Process.
        context_requirements: What context this process needs compiled at run
            time (``None`` = minimal context).
        provides_capabilities: What this process can *accomplish*, as
            capability references (Phase 4A).  Declaring a competence here is
            how work finds an implementation without anyone maintaining a
            name→process table.  The relation itself is persisted in
            ``process_capabilities``; this field is the declaration.
    """

    name: str
    version: str
    handler: str
    trigger_event_types: tuple[str, ...] = ()
    max_retries: int = 0
    metadata: dict = field(default_factory=dict)
    context_requirements: ContextRequirements | None = None
    provides_capabilities: tuple = ()


@dataclass
class ProcessInstance:
    """A concrete, running (or paused/finished) process.

    Attributes:
        definition_name: Name of the :class:`ProcessDefinition`.
        definition_version: Version of the definition.
        id: Unique identifier.
        status: Current :class:`ProcessStatus`.
        input: The input the instance was created with.
        local_state: Mutable per-instance working state.
        parent_process_id: The spawning instance, if any.
        priority: Higher runs first when several are RUNNABLE.
        pending_event_id: Runtime coordination field — the event that will
            (re)activate this instance on its next run.  Persisted so activation
            survives a runtime restart.
        work_key: Logical work identity this process fulfils (if any).
        work_requirement_id: The WorkRequirement this process fulfils (if any).
        retry_count: How many retries have been attempted.
        max_retries: Retry budget copied from the definition.
        next_retry_at: When a RETRY_WAIT instance becomes RUNNABLE again.
        last_error: Message from the most recent failure.
        created_at: When the instance was created (UTC).
        updated_at: When the instance was last updated (UTC).
    """

    definition_name: str
    definition_version: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: ProcessStatus = ProcessStatus.RUNNABLE
    input: dict = field(default_factory=dict)
    local_state: dict = field(default_factory=dict)
    parent_process_id: uuid.UUID | None = None
    priority: int = 0
    pending_event_id: uuid.UUID | None = None
    work_key: str | None = None
    work_requirement_id: uuid.UUID | None = None
    #: The event that *created* this instance.  Unlike ``pending_event_id``
    #: (which is cleared once an activation commits) this is permanent, so
    #: re-dispatching an event can tell it already triggered this process.
    trigger_event_id: uuid.UUID | None = None
    #: The composed plan and position this instance is filling, if any.
    plan_id: uuid.UUID | None = None
    plan_node_id: uuid.UUID | None = None
    retry_count: int = 0
    max_retries: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SpawnSpec:
    """A request to spawn a child process."""

    definition_name: str
    definition_version: str
    input: dict = field(default_factory=dict)
    priority: int = 0
    correlation_id: uuid.UUID | None = None
    work_key: str | None = None
    work_requirement_id: uuid.UUID | None = None
    #: The plan position this child fills, if it is part of a composed plan.
    #: The runtime links the created instance back to the node — mechanism, not
    #: planning knowledge.
    plan_id: uuid.UUID | None = None
    plan_node_id: uuid.UUID | None = None


@dataclass
class TimerSpec:
    """A request to arm a timer that will emit a ``timer_fired`` event."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    delay: float | None = None
    fire_at: datetime | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class JoinRequest:
    """A request to suspend the parent until spawned children finish.

    Attributes:
        mode: ``"all"`` waits for every child; ``"any"`` for the first.
        resume_point: Where the parent resumes once the join is satisfied.
        saved_process_state: State handed back to the parent on resume.
    """

    mode: str
    resume_point: str
    saved_process_state: dict = field(default_factory=dict)


@dataclass
class ProcessResult:
    """The outcome of running a process handler — a batch of effects.

    The runtime applies all of these in a single transaction.

    Attributes:
        status: The status the instance should transition to.
        output: Optional structured output.
        state_changes: World-state writes to apply.
        emitted_events: New events the process produced.
        continuations_to_create: Continuations to persist (e.g. on suspend).
        continuations_to_delete: Continuation ids to remove.
        spawned_processes: Child processes to create.
        timers_to_create: Timers to arm.
        observations: Observations to persist.
        state_deltas: State deltas to persist.
        work_requirements: Work requirements to persist (insert-or-ignore by key).
        work_requirement_updates: ``(requirement_id, new_status)`` transitions.
        action_proposals: Action proposals to persist (never executed here — a
            proposal is a *candidate*; the action subsystem authorizes it).
        action_proposal_updates: ``(proposal_id, new_status)`` transitions.
        action_executions: Attempt records for the external-effect journal.
        action_decisions: Authorization decisions (permission provenance).
        resources: Resources to persist (upsert by id).
        resource_versions: Immutable ResourceVersions to append.
        resource_representations: Extraction results to persist.
        join: Optional join request (suspend until children finish).
        retryable: If FAILED, whether the failure may be retried.
        retry_delay: Optional explicit backoff (seconds) for a retry.

    Most fields are discarded when the result is FAILED — a failed activation
    must leave no partial effects.  The two *journals*
    (:attr:`llm_invocations`, :attr:`action_executions`) are the exception: they
    record what was *attempted*, so they are persisted on failure too
    (Invariant 26).
    """

    status: ProcessStatus
    output: dict | None = None
    state_changes: list[StateChange] = field(default_factory=list)
    emitted_events: list[Event] = field(default_factory=list)
    continuations_to_create: list[Continuation] = field(default_factory=list)
    continuations_to_delete: list[uuid.UUID] = field(default_factory=list)
    spawned_processes: list[SpawnSpec] = field(default_factory=list)
    timers_to_create: list[TimerSpec] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    state_deltas: list[StateDelta] = field(default_factory=list)
    work_requirements: list[WorkRequirement] = field(default_factory=list)
    work_requirement_updates: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    proposals: list["InterpretationProposal"] = field(default_factory=list)
    proposal_updates: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    llm_invocations: list["LLMInvocation"] = field(default_factory=list)
    action_proposals: list["ActionProposal"] = field(default_factory=list)
    action_proposal_updates: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    action_executions: list["ActionExecution"] = field(default_factory=list)
    action_decisions: list["ActionDecisionRecord"] = field(default_factory=list)
    resources: list["Resource"] = field(default_factory=list)
    resource_versions: list["ResourceVersion"] = field(default_factory=list)
    resource_representations: list["ResourceRepresentation"] = field(default_factory=list)
    #: ``(requirement_id, status, missing_capabilities, selected_definition)``
    work_matches: list[tuple] = field(default_factory=list)
    capability_matches: list = field(default_factory=list)
    plans_to_create: list = field(default_factory=list)
    #: ``(plan_id, status)``
    plan_updates: list[tuple] = field(default_factory=list)
    #: ``(node_id, status, process_instance_id)``
    plan_node_updates: list[tuple] = field(default_factory=list)
    #: Phase 4C decision layer.
    plan_evaluations: list = field(default_factory=list)
    plan_selection_proposals: list = field(default_factory=list)
    #: ``(proposal_id, status, reasons)``
    plan_selection_proposal_updates: list[tuple] = field(default_factory=list)
    plan_selections: list = field(default_factory=list)
    replan_attempts: list = field(default_factory=list)
    #: ``(work_requirement_id, attempt_number)``
    replan_counts: list[tuple] = field(default_factory=list)
    #: Phase 5A self-extension.  Held apart from ``work`` on purpose: a
    #: CapabilityGap is a deficiency of ours, not a change to the need
    #: (Invariant 85).
    capability_gaps: list = field(default_factory=list)
    #: ``(capability_gap_id, status)``
    capability_gap_updates: list[tuple] = field(default_factory=list)
    extension_proposals: list = field(default_factory=list)
    #: ``(proposal_id, status, reasons)``
    extension_proposal_updates: list[tuple] = field(default_factory=list)
    extension_decisions: list = field(default_factory=list)
    #: Phase 5B sandboxed construction effects.
    construction_plans: list = field(default_factory=list)
    construction_plan_updates: list[tuple] = field(default_factory=list)
    construction_step_updates: list[tuple] = field(default_factory=list)
    sandbox_workspaces: list = field(default_factory=list)
    sandbox_workspace_updates: list[tuple] = field(default_factory=list)
    construction_grants: list = field(default_factory=list)
    construction_grant_updates: list[tuple] = field(default_factory=list)
    verification_checks: list = field(default_factory=list)
    construction_results: list = field(default_factory=list)
    #: Phase 5C installation/activation effects.
    installation_plans: list = field(default_factory=list)
    #: ``(plan_id, status, reasons)``
    installation_plan_updates: list[tuple] = field(default_factory=list)
    installation_step_updates: list[tuple] = field(default_factory=list)
    installation_grants: list = field(default_factory=list)
    #: ``(grant_id, status, revoked_at)``
    installation_grant_updates: list[tuple] = field(default_factory=list)
    installation_checks: list = field(default_factory=list)
    installation_results: list = field(default_factory=list)
    installation_decisions: list = field(default_factory=list)
    #: ``(ActivationRecord, ProcessDefinition, list[Capability])``
    installation_activations: list = field(default_factory=list)
    rollback_records: list = field(default_factory=list)
    #: Phase 5D bounded capability-acquisition coordination.
    acquisition_sessions: list = field(default_factory=list)
    acquisition_subscribers: list = field(default_factory=list)
    autonomy_decisions: list = field(default_factory=list)
    acquisition_attempts: list = field(default_factory=list)
    #: Explicit closure of another suspended instance whose continuation ended.
    process_instance_updates: list[tuple] = field(default_factory=list)
    join: JoinRequest | None = None
    retryable: bool = False
    retry_delay: float | None = None

    # --- structured views (Phase 4B) --------------------------------------
    #
    # The lists above stay flat so every existing handler and test keeps
    # working (spec §77).  These views group them by the layer that owns each,
    # so the whole batch can be read and reasoned about as a shape rather than
    # as twenty-odd unrelated attributes.

    @property
    def lifecycle(self) -> "ProcessLifecycleResult":
        """What happens to the instance, separate from what it wrote."""
        from .effects import ProcessLifecycleResult

        return ProcessLifecycleResult(
            status=self.status,
            output=self.output,
            retryable=self.retryable,
            retry_delay=self.retry_delay,
            join=self.join,
        )

    @property
    def effects(self) -> "ProcessEffects":
        """Everything this activation wrote, grouped by owning layer."""
        from .effects import (
            ActionEffects,
            AutonomyEffects,
            ConstructionEffects,
            DecisionEffects,
            ExtensionEffects,
            IntelligenceEffects,
            InstallationEffects,
            PlanningEffects,
            ProcessEffects,
            ResourceEffects,
            SemanticEffects,
            WorkEffects,
        )

        return ProcessEffects(
            events=self.emitted_events,
            state=self.state_changes,
            continuations_to_create=self.continuations_to_create,
            continuations_to_delete=self.continuations_to_delete,
            processes=self.spawned_processes,
            timers=self.timers_to_create,
            semantic=SemanticEffects(
                observations=self.observations, state_deltas=self.state_deltas
            ),
            work=WorkEffects(
                requirements=self.work_requirements,
                status_updates=self.work_requirement_updates,
                matches=self.work_matches,
                capability_matches=self.capability_matches,
            ),
            intelligence=IntelligenceEffects(
                proposals=self.proposals,
                proposal_updates=self.proposal_updates,
                llm_invocations=self.llm_invocations,
            ),
            actions=ActionEffects(
                proposals=self.action_proposals,
                proposal_updates=self.action_proposal_updates,
                executions=self.action_executions,
                decisions=self.action_decisions,
            ),
            resources=ResourceEffects(
                resources=self.resources,
                versions=self.resource_versions,
                representations=self.resource_representations,
            ),
            planning=PlanningEffects(
                plans_to_create=self.plans_to_create,
                plan_updates=self.plan_updates,
                node_updates=self.plan_node_updates,
            ),
            decision=DecisionEffects(
                evaluations=self.plan_evaluations,
                selection_proposals=self.plan_selection_proposals,
                selection_proposal_updates=self.plan_selection_proposal_updates,
                selections=self.plan_selections,
                replan_attempts=self.replan_attempts,
                replan_counts=self.replan_counts,
            ),
            extension=ExtensionEffects(
                gaps=self.capability_gaps,
                gap_updates=self.capability_gap_updates,
                proposals=self.extension_proposals,
                proposal_updates=self.extension_proposal_updates,
                decisions=self.extension_decisions,
            ),
            construction=ConstructionEffects(
                plans=self.construction_plans,
                plan_updates=self.construction_plan_updates,
                step_updates=self.construction_step_updates,
                workspaces=self.sandbox_workspaces,
                workspace_updates=self.sandbox_workspace_updates,
                grants=self.construction_grants,
                grant_updates=self.construction_grant_updates,
                verification_checks=self.verification_checks,
                results=self.construction_results,
            ),
            installation=InstallationEffects(
                plans=self.installation_plans,
                plan_updates=self.installation_plan_updates,
                step_updates=self.installation_step_updates,
                grants=self.installation_grants,
                grant_updates=self.installation_grant_updates,
                checks=self.installation_checks,
                results=self.installation_results,
                decisions=self.installation_decisions,
                activations=self.installation_activations,
                rollbacks=self.rollback_records,
            ),
            autonomy=AutonomyEffects(
                sessions=self.acquisition_sessions,
                subscribers=self.acquisition_subscribers,
                decisions=self.autonomy_decisions,
                attempts=self.acquisition_attempts,
            ),
        )


@dataclass
class ProcessContext:
    """The handle passed to a process handler — its whole view of the world.

    A handler reads/writes world state through :attr:`state` (a
    :class:`~nexus_seed.core.state.StateView`; writes are staged), inspects the
    triggering/resuming :attr:`event`, and returns a :class:`ProcessResult`
    built with :meth:`complete`, :meth:`suspend`, :meth:`suspend_on_timer`,
    :meth:`spawn_and_join`, :meth:`retry` or :meth:`fail`.
    """

    instance: ProcessInstance
    event: Event | None
    state: StateView
    view: "ProcessContextView | None" = None
    context: Context | None = None
    resume_point: str | None = None
    saved_process_state: dict = field(default_factory=dict)
    services: object | None = None
    backends: dict = field(default_factory=dict)
    #: Registered external adapters, for an observer process to poll.
    adapters: object | None = None
    #: The ingress boundary, so an observer can take what it polled *in*.
    ingress: object | None = None
    #: The Project Orchestrator, for the one Process that carries a request to
    #: it.  A boundary a handler may drive, like ``ingress`` — deliberately not
    #: on ``services``, which stays reads-only.
    project_orchestrator: object | None = None
    context_snapshot_id: uuid.UUID | None = None
    activation_id: str | None = None
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("nexus_seed.process")
    )
    _observations: list[Observation] = field(default_factory=list)
    _state_deltas: list[StateDelta] = field(default_factory=list)
    _work_requirements: list[WorkRequirement] = field(default_factory=list)
    _work_requirement_updates: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    _proposals: list["InterpretationProposal"] = field(default_factory=list)
    _proposal_updates: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    _llm_invocations: list["LLMInvocation"] = field(default_factory=list)
    _action_proposals: list["ActionProposal"] = field(default_factory=list)
    _action_proposal_updates: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    _action_executions: list["ActionExecution"] = field(default_factory=list)
    _action_decisions: list["ActionDecisionRecord"] = field(default_factory=list)
    _resources: list["Resource"] = field(default_factory=list)
    _resource_versions: list["ResourceVersion"] = field(default_factory=list)
    _resource_representations: list["ResourceRepresentation"] = field(default_factory=list)
    _work_matches: list[tuple] = field(default_factory=list)
    _capability_matches: list = field(default_factory=list)
    _plans_to_create: list = field(default_factory=list)
    _plan_updates: list[tuple] = field(default_factory=list)
    _plan_node_updates: list[tuple] = field(default_factory=list)
    _plan_evaluations: list = field(default_factory=list)
    _plan_selection_proposals: list = field(default_factory=list)
    _plan_selection_proposal_updates: list[tuple] = field(default_factory=list)
    _plan_selections: list = field(default_factory=list)
    _replan_attempts: list = field(default_factory=list)
    _replan_counts: list[tuple] = field(default_factory=list)
    _capability_gaps: list = field(default_factory=list)
    _capability_gap_updates: list[tuple] = field(default_factory=list)
    _extension_proposals: list = field(default_factory=list)
    _extension_proposal_updates: list[tuple] = field(default_factory=list)
    _extension_decisions: list = field(default_factory=list)
    _construction_plans: list = field(default_factory=list)
    _construction_plan_updates: list[tuple] = field(default_factory=list)
    _construction_step_updates: list[tuple] = field(default_factory=list)
    _sandbox_workspaces: list = field(default_factory=list)
    _sandbox_workspace_updates: list[tuple] = field(default_factory=list)
    _construction_grants: list = field(default_factory=list)
    _construction_grant_updates: list[tuple] = field(default_factory=list)
    _verification_checks: list = field(default_factory=list)
    _construction_results: list = field(default_factory=list)
    _installation_plans: list = field(default_factory=list)
    _installation_plan_updates: list[tuple] = field(default_factory=list)
    _installation_step_updates: list[tuple] = field(default_factory=list)
    _installation_grants: list = field(default_factory=list)
    _installation_grant_updates: list[tuple] = field(default_factory=list)
    _installation_checks: list = field(default_factory=list)
    _installation_results: list = field(default_factory=list)
    _installation_decisions: list = field(default_factory=list)
    _installation_activations: list = field(default_factory=list)
    _rollback_records: list = field(default_factory=list)
    _acquisition_sessions: list = field(default_factory=list)
    _acquisition_subscribers: list = field(default_factory=list)
    _autonomy_decisions: list = field(default_factory=list)
    _acquisition_attempts: list = field(default_factory=list)
    _continuations_to_delete: list[uuid.UUID] = field(default_factory=list)
    _process_instance_updates: list[tuple] = field(default_factory=list)

    @property
    def correlation_id(self) -> uuid.UUID | None:
        """Correlation id to propagate onto events this process emits."""
        if self.event is not None:
            return self.event.correlation_id or self.event.id
        cid = self.instance.input.get("correlation_id")
        return uuid.UUID(cid) if isinstance(cid, str) else None

    def new_event(
        self,
        type: str,
        payload: dict | None = None,
        *,
        source: str | None = None,
    ) -> Event:
        """Create an event caused by the current activation.

        The new event inherits this activation's ``correlation_id`` and records
        the triggering event as its ``causation_id`` so causal chains are kept.
        """
        return Event(
            type=type,
            source=source or self.instance.definition_name,
            payload=payload or {},
            correlation_id=self.correlation_id,
            causation_id=self.event.id if self.event else None,
        )

    def observe(
        self,
        *,
        subject: str,
        predicate: str,
        extracted: dict,
        confidence: float = 1.0,
        proposal_id: uuid.UUID | None = None,
        source_event_id: uuid.UUID | None = None,
    ) -> Observation:
        """Record a semantic reading of the current event (staged on the result)."""
        observation = Observation(
            subject=subject,
            predicate=predicate,
            extracted=dict(extracted),
            source_event_id=source_event_id
            or (self.event.id if self.event else None),
            created_by_process_id=self.instance.id,
            confidence=confidence,
            proposal_id=proposal_id,
        )
        self._observations.append(observation)
        return observation

    def add_observation(self, observation: Observation) -> Observation:
        """Stage a pre-built observation (used when its source event differs)."""
        self._observations.append(observation)
        return observation

    def add_state_delta(self, delta: StateDelta) -> StateDelta:
        """Stage a pre-built state delta (used when its source event differs)."""
        self._state_deltas.append(delta)
        return delta

    def record_proposal(self, proposal) -> object:
        """Stage an interpretation proposal for persistence."""
        self._proposals.append(proposal)
        return proposal

    def update_proposal(self, proposal_id: uuid.UUID, decision) -> None:
        """Stage a proposal decision transition."""
        value = getattr(decision, "value", decision)
        self._proposal_updates.append((proposal_id, value))

    def record_llm_invocation(self, invocation) -> object:
        """Stage an LLM invocation audit record."""
        self._llm_invocations.append(invocation)
        return invocation

    def propose_action(
        self,
        *,
        backend: str,
        action_type: str,
        target: str | None = None,
        parameters: dict | None = None,
        required_permissions: list[str] | None = None,
        declared_side_effects: list[str] | None = None,
        risk_level="LOW",
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ):
        """Propose an external action (staged; **not** executed here).

        This is the only way a Process may reach the outside world: a handler
        never calls a side-effecting backend itself (Invariants 21–22).  What is
        returned is a *candidate* — the action subsystem still has to validate
        it, check permissions and apply risk policy before anything runs.

        The proposal automatically carries this activation's provenance: the
        proposing instance, its WorkRequirement, the trigger event and the
        compiled ContextSnapshot the decision was made against.
        """
        from ..actions.models import ActionProposal, RiskLevel

        level = RiskLevel.coerce(risk_level)
        if level is None:
            raise ValueError(f"invalid risk_level {risk_level!r}")

        proposal = ActionProposal(
            backend=backend,
            action_type=action_type,
            target=target,
            parameters=dict(parameters or {}),
            required_permissions=list(required_permissions or []),
            declared_side_effects=list(declared_side_effects or []),
            risk_level=level,
            rationale=rationale,
            created_by_process_id=self.instance.id,
            source_work_requirement_id=self.instance.work_requirement_id,
            trigger_event_id=self.event.id if self.event else None,
            context_snapshot_id=self.context_snapshot_id,
            idempotency_key=idempotency_key,
        )
        self._action_proposals.append(proposal)
        return proposal

    def add_action_proposal(self, proposal):
        """Stage a pre-built action proposal (e.g. a review replacement)."""
        self._action_proposals.append(proposal)
        return proposal

    def update_action_proposal(self, proposal_id: uuid.UUID, status) -> None:
        """Stage an action-proposal status transition (applied atomically)."""
        self._action_proposal_updates.append((proposal_id, getattr(status, "value", status)))

    def record_action_execution(self, execution):
        """Stage an action-execution journal entry (kept even if this fails)."""
        self._action_executions.append(execution)
        return execution

    def record_action_decision(self, decision):
        """Stage an authorization decision record (permission provenance)."""
        self._action_decisions.append(decision)
        return decision

    def add_resource(self, resource):
        """Stage a Resource for persistence (upsert by id)."""
        self._resources.append(resource)
        return resource

    def add_resource_version(self, version):
        """Stage an immutable ResourceVersion for persistence."""
        self._resource_versions.append(version)
        return version

    def add_representation(self, representation):
        """Stage an extraction result for persistence.

        Extraction is a Process, so its output travels the same declarative
        route as every other effect (Invariant 38) — the handler never writes
        to the database itself.
        """
        if representation.created_by_process_id is None:
            representation.created_by_process_id = self.instance.id
        self._resource_representations.append(representation)
        return representation


    def propose_delta(
        self,
        *,
        entity: str,
        attribute: str,
        old_value: object,
        new_value: object,
        observation: Observation | None = None,
        confidence: float = 1.0,
        reason: str | None = None,
    ) -> StateDelta:
        """Propose a world-state change (staged on the result; not applied here)."""
        delta = StateDelta(
            entity=entity,
            attribute=attribute,
            old_value=old_value,
            new_value=new_value,
            source_event_id=self.event.id if self.event else None,
            observation_id=observation.id if observation else None,
            created_by_process_id=self.instance.id,
            confidence=confidence,
            reason=reason,
        )
        self._state_deltas.append(delta)
        return delta

    def require_work(
        self,
        *,
        work_type: str,
        work_key: str,
        related_entities: list[str] | None = None,
        reason: str = "",
        source_state_delta_id: uuid.UUID | None = None,
        priority: int = 0,
        metadata: dict | None = None,
        required_capabilities: list | None = None,
        available_input_types: list[str] | None = None,
        required_output_types: list[str] | None = None,
    ) -> WorkRequirement:
        """Declare that a unit of work is required (staged on the result).

        ``required_capabilities`` says what doing the work *takes*; leaving it
        empty falls back to the legacy ``work_type`` lookup.
        """
        from ..capabilities.models import CapabilityRequirement

        requirement = WorkRequirement(
            work_type=work_type,
            work_key=work_key,
            related_entities=list(related_entities or []),
            reason=reason,
            source_event_id=self.event.id if self.event else None,
            source_state_delta_id=source_state_delta_id,
            priority=priority,
            metadata=dict(metadata or {}),
            required_capabilities=[
                CapabilityRequirement.coerce(r) for r in (required_capabilities or [])
            ],
            available_input_types=list(available_input_types or []),
            required_output_types=list(required_output_types or []),
        )
        self._work_requirements.append(requirement)
        return requirement

    def add_work_requirement(self, requirement: WorkRequirement) -> WorkRequirement:
        """Stage a pre-built WorkRequirement with extended domain metadata."""
        self._work_requirements.append(requirement)
        return requirement

    def mark_work(self, requirement_id: uuid.UUID, status: WorkStatus | str) -> None:
        """Stage a WorkRequirement status transition (applied atomically)."""
        value = status.value if isinstance(status, WorkStatus) else status
        self._work_requirement_updates.append((requirement_id, value))

    def match_work(
        self,
        requirement_id: uuid.UUID,
        *,
        status=None,
        missing_capabilities: list[str] | None = None,
        selected_definition: tuple[str, str] | None = None,
    ) -> None:
        """Stage the outcome of capability matching on a WorkRequirement.

        ``status=None`` records the selection without touching the lifecycle.
        """
        self._work_matches.append(
            (
                requirement_id,
                getattr(status, "value", status) if status is not None else None,
                list(missing_capabilities or []),
                selected_definition,
            )
        )

    def record_capability_match(self, match):
        """Stage a capability matching attempt for the audit history."""
        self._capability_matches.append(match)
        return match

    def create_plan(self, plan, nodes, edges):
        """Stage a composed ProcessPlan with its nodes and edges (Phase 4B).

        The three land in one transaction, so a partially-written plan cannot
        exist (spec §11).
        """
        self._plans_to_create.append((plan, list(nodes), list(edges)))
        return plan

    def update_plan(self, plan_id: uuid.UUID, status) -> None:
        """Stage a plan status transition."""
        self._plan_updates.append((plan_id, getattr(status, "value", status)))

    def update_plan_node(
        self, node_id: uuid.UUID, status, *, process_instance_id: uuid.UUID | None = None
    ) -> None:
        """Stage a plan node transition, optionally binding its instance."""
        self._plan_node_updates.append(
            (node_id, getattr(status, "value", status), process_instance_id)
        )

    # --- decision layer (Phase 4C) -----------------------------------------

    def record_plan_evaluation(self, evaluation):
        """Stage what one candidate plan is estimated to cost, take and risk."""
        self._plan_evaluations.append(evaluation)
        return evaluation

    def record_selection_proposal(self, proposal):
        """Stage an LLM's *suggestion* about which plan to run."""
        self._plan_selection_proposals.append(proposal)
        return proposal

    def update_selection_proposal(self, proposal_id, status, *, reasons=None) -> None:
        """Stage a transition of a selection proposal, with the reason for it."""
        self._plan_selection_proposal_updates.append(
            (proposal_id, getattr(status, "value", status), list(reasons or []))
        )

    def record_plan_selection(self, selection):
        """Stage the decision itself — this plan, for this need (spec §36).

        Append-only: a later selection is a new row, never an edit of this one
        (Invariant 78).
        """
        self._plan_selections.append(selection)
        return selection

    def record_replan_attempt(self, attempt):
        """Stage one attempt to find another way after a plan failed."""
        self._replan_attempts.append(attempt)
        return attempt

    def count_replan(self, work_requirement_id, attempt_number: int) -> None:
        """Stage the need's replan counter, which bounds further attempts."""
        self._replan_counts.append((work_requirement_id, attempt_number))

    # --- self-extension layer (Phase 5A) -----------------------------------

    def open_capability_gap(self, gap):
        """Stage a CapabilityGap — *we* are missing something (Invariant 85).

        Deliberately not a work update: the need is unchanged and keeps its
        status, its provenance and its id.  What is being recorded is a
        deficiency on our side of the boundary, and recording it grants no
        authority to do anything about it (Invariant 84).
        """
        self._capability_gaps.append(gap)
        return gap

    def update_capability_gap(self, gap_id: uuid.UUID, status) -> None:
        """Stage a CapabilityGap transition (applied atomically)."""
        self._capability_gap_updates.append((gap_id, getattr(status, "value", status)))

    def record_extension_proposal(self, proposal):
        """Stage a proposal about how a gap might be closed.

        A *description*, never an applied change: staging this registers no
        capability, writes no file and installs nothing (Invariant 90).
        """
        self._extension_proposals.append(proposal)
        return proposal

    def update_extension_proposal(self, proposal_id: uuid.UUID, status, *, reasons=None) -> None:
        """Stage an extension-proposal transition, with the reason for it."""
        self._extension_proposal_updates.append(
            (proposal_id, getattr(status, "value", status), list(reasons or []))
        )

    def record_extension_decision(self, decision):
        """Stage why an extension proposal was approved, reviewed or refused.

        Append-only (Invariant 92): a second decision is a second row, never an
        edit of the first.
        """
        self._extension_decisions.append(decision)
        return decision

    # --- sandboxed construction (Phase 5B) --------------------------------

    def record_construction_plan(self, plan):
        """Stage a validated HOW without executing or activating it."""
        self._construction_plans.append(plan)
        return plan

    def update_construction_plan(self, plan_id, status) -> None:
        self._construction_plan_updates.append((plan_id, getattr(status, "value", status)))

    def update_construction_step(self, step_id, status) -> None:
        self._construction_step_updates.append((step_id, getattr(status, "value", status)))

    def record_sandbox_workspace(self, workspace):
        self._sandbox_workspaces.append(workspace)
        return workspace

    def update_sandbox_workspace(self, workspace_id, status, *, closed_at=None) -> None:
        self._sandbox_workspace_updates.append(
            (workspace_id, getattr(status, "value", status), closed_at)
        )

    def record_construction_grant(self, grant):
        self._construction_grants.append(grant)
        return grant

    def update_construction_grant(self, grant_id, status) -> None:
        self._construction_grant_updates.append((grant_id, getattr(status, "value", status)))

    def record_verification_check(self, check):
        self._verification_checks.append(check)
        return check

    def record_construction_result(self, result):
        self._construction_results.append(result)
        return result

    # --- production installation / activation (Phase 5C) -----------------

    def record_installation_plan(self, plan):
        self._installation_plans.append(plan)
        return plan

    def update_installation_plan(self, plan_id, status, *, reasons=None) -> None:
        self._installation_plan_updates.append(
            (plan_id, getattr(status, "value", status), list(reasons or []))
        )

    def update_installation_step(self, step_id, status) -> None:
        self._installation_step_updates.append((step_id, getattr(status, "value", status)))

    def record_installation_grant(self, grant):
        self._installation_grants.append(grant)
        return grant

    def update_installation_grant(self, grant_id, status, *, revoked_at=None) -> None:
        self._installation_grant_updates.append(
            (grant_id, getattr(status, "value", status), revoked_at)
        )

    def record_installation_check(self, check):
        self._installation_checks.append(check)
        return check

    def record_installation_result(self, result):
        self._installation_results.append(result)
        return result

    def record_installation_decision(self, decision):
        self._installation_decisions.append(decision)
        return decision

    def activate_installation(self, record, definition, capabilities):
        """Stage component + definition + capability publication atomically."""
        self._installation_activations.append((record, definition, list(capabilities)))
        return record

    def record_rollback(self, record):
        self._rollback_records.append(record)
        return record

    # --- bounded capability acquisition (Phase 5D) -----------------------

    def record_acquisition_session(self, session):
        """Stage a durable coordinator record; no phase boundary is bypassed."""
        self._acquisition_sessions.append(session)
        return session

    def record_acquisition_subscriber(self, subscriber):
        """Attach one WorkRequirement to a shared logical acquisition."""
        self._acquisition_subscribers.append(subscriber)
        return subscriber

    def record_autonomy_decision(self, decision):
        """Append why a stage was automatic, reviewable, or forbidden."""
        self._autonomy_decisions.append(decision)
        return decision

    def record_acquisition_attempt(self, attempt):
        """Stage one bounded logical construction/installation attempt."""
        self._acquisition_attempts.append(attempt)
        return attempt

    def close_continuation(
        self,
        continuation_id: uuid.UUID,
        *,
        process_instance_id: uuid.UUID | None = None,
        reason: str = "continuation closed",
    ) -> None:
        """Stage closure of another logical waiter in the same transaction."""
        self._continuations_to_delete.append(continuation_id)
        if process_instance_id is not None:
            self._process_instance_updates.append(
                (process_instance_id, ProcessStatus.COMPLETED.value, {"reason": reason})
            )

    def satisfy_work(self, evidence: dict | None = None) -> bool:
        """Mark Work SATISFIED only when its structured criteria are met.

        Existing work has no explicit criteria and behaves exactly as before.
        Human-created work may name evidence, state predicates, or artifact
        availability; Process completion alone does not satisfy those needs.
        """
        if self.instance.work_requirement_id is None:
            return False
        requirement = (
            self.services.get_work_requirement(self.instance.work_requirement_id)
            if self.services is not None
            else None
        )
        if requirement is not None and requirement.completion_criteria:
            supplied = evidence or {}
            unmet = [
                criterion
                for criterion in requirement.completion_criteria
                if not self._completion_criterion_met(criterion, supplied)
            ]
            if unmet:
                self.logger.info(
                    "work %s completed a Process but has unmet criteria: %s",
                    requirement.work_key,
                    unmet,
                )
                return False
        self.mark_work(self.instance.work_requirement_id, WorkStatus.SATISFIED)
        return True

    def _completion_criterion_met(self, criterion, evidence: dict) -> bool:
        if isinstance(criterion, str):
            return bool(evidence.get(criterion))
        if not isinstance(criterion, dict):
            return False
        kind = str(criterion.get("type") or "evidence")
        if kind == "evidence":
            return bool(evidence.get(str(criterion.get("name"))))
        if kind == "state_predicate" and self.services is not None:
            entry = self.services.get_current_state(
                str(criterion.get("entity")), str(criterion.get("attribute"))
            )
            return bool(entry and entry.value == criterion.get("value"))
        if kind == "artifact_available" and self.services is not None:
            return self.services.get_resource_by_uri(str(criterion.get("uri"))) is not None
        return False

    def _staged(self) -> dict:
        """Every effect staged on this context, as ProcessResult kwargs."""
        return {
            "observations": list(self._observations),
            "state_deltas": list(self._state_deltas),
            "work_requirements": list(self._work_requirements),
            "work_requirement_updates": list(self._work_requirement_updates),
            "proposals": list(self._proposals),
            "proposal_updates": list(self._proposal_updates),
            "llm_invocations": list(self._llm_invocations),
            "action_proposals": list(self._action_proposals),
            "action_proposal_updates": list(self._action_proposal_updates),
            "action_executions": list(self._action_executions),
            "action_decisions": list(self._action_decisions),
            "resources": list(self._resources),
            "resource_versions": list(self._resource_versions),
            "resource_representations": list(self._resource_representations),
            "work_matches": list(self._work_matches),
            "capability_matches": list(self._capability_matches),
            "plans_to_create": list(self._plans_to_create),
            "plan_updates": list(self._plan_updates),
            "plan_node_updates": list(self._plan_node_updates),
            "plan_evaluations": list(self._plan_evaluations),
            "plan_selection_proposals": list(self._plan_selection_proposals),
            "plan_selection_proposal_updates": list(
                self._plan_selection_proposal_updates
            ),
            "plan_selections": list(self._plan_selections),
            "replan_attempts": list(self._replan_attempts),
            "replan_counts": list(self._replan_counts),
            "capability_gaps": list(self._capability_gaps),
            "capability_gap_updates": list(self._capability_gap_updates),
            "extension_proposals": list(self._extension_proposals),
            "extension_proposal_updates": list(self._extension_proposal_updates),
            "extension_decisions": list(self._extension_decisions),
            "construction_plans": list(self._construction_plans),
            "construction_plan_updates": list(self._construction_plan_updates),
            "construction_step_updates": list(self._construction_step_updates),
            "sandbox_workspaces": list(self._sandbox_workspaces),
            "sandbox_workspace_updates": list(self._sandbox_workspace_updates),
            "construction_grants": list(self._construction_grants),
            "construction_grant_updates": list(self._construction_grant_updates),
            "verification_checks": list(self._verification_checks),
            "construction_results": list(self._construction_results),
            "installation_plans": list(self._installation_plans),
            "installation_plan_updates": list(self._installation_plan_updates),
            "installation_step_updates": list(self._installation_step_updates),
            "installation_grants": list(self._installation_grants),
            "installation_grant_updates": list(self._installation_grant_updates),
            "installation_checks": list(self._installation_checks),
            "installation_results": list(self._installation_results),
            "installation_decisions": list(self._installation_decisions),
            "installation_activations": list(self._installation_activations),
            "rollback_records": list(self._rollback_records),
            "acquisition_sessions": list(self._acquisition_sessions),
            "acquisition_subscribers": list(self._acquisition_subscribers),
            "autonomy_decisions": list(self._autonomy_decisions),
            "acquisition_attempts": list(self._acquisition_attempts),
            "continuations_to_delete": list(self._continuations_to_delete),
            "process_instance_updates": list(self._process_instance_updates),
        }

    def _staged_journals(self) -> dict:
        """Only the attempt journals — the effects that survive a failure."""
        return {
            "llm_invocations": list(self._llm_invocations),
            "action_executions": list(self._action_executions),
        }

    def complete(
        self,
        output: dict | None = None,
        *,
        emitted_events: list[Event] | None = None,
        spawned_processes: list[SpawnSpec] | None = None,
    ) -> ProcessResult:
        """Return a result marking the process COMPLETED."""
        return ProcessResult(
            status=ProcessStatus.COMPLETED,
            output=output,
            state_changes=list(self.state.changes),
            emitted_events=list(emitted_events or []),
            spawned_processes=list(spawned_processes or []),
            **self._staged(),
        )

    def suspend(
        self,
        *,
        resume_point: str,
        waiting_for: dict,
        saved_process_state: dict | None = None,
        context_ref: str | None = None,
        emitted_events: list[Event] | None = None,
    ) -> ProcessResult:
        """Return a result marking the process SUSPENDED with a continuation."""
        continuation = Continuation(
            process_instance_id=self.instance.id,
            resume_point=resume_point,
            waiting_for=dict(waiting_for),
            saved_process_state=dict(saved_process_state or {}),
            context_ref=context_ref,
        )
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            state_changes=list(self.state.changes),
            emitted_events=list(emitted_events or []),
            continuations_to_create=[continuation],
            **self._staged(),
        )

    def suspend_on_timer(
        self,
        *,
        resume_point: str,
        delay: float | None = None,
        fire_at: datetime | None = None,
        saved_process_state: dict | None = None,
        also_waiting_for: list[dict] | None = None,
        emitted_events: list[Event] | None = None,
    ) -> ProcessResult:
        """Suspend until a timer fires (``delay`` seconds from now, or ``fire_at``).

        ``also_waiting_for`` adds alternative wake-up conditions alongside the
        timer — the basis of a long-lived observer that ticks on a schedule but
        can also be stopped by an event (spec §39, §48).
        """
        timer = TimerSpec(delay=delay, fire_at=fire_at)
        timer.payload = {"timer_id": str(timer.id)}
        on_timer = {"event_type": "timer_fired", "timer_id": str(timer.id)}
        waiting_for = (
            {"any": [on_timer, *also_waiting_for]} if also_waiting_for else on_timer
        )
        continuation = Continuation(
            process_instance_id=self.instance.id,
            resume_point=resume_point,
            waiting_for=waiting_for,
            saved_process_state=dict(saved_process_state or {}),
        )
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            state_changes=list(self.state.changes),
            emitted_events=list(emitted_events or []),
            continuations_to_create=[continuation],
            timers_to_create=[timer],
            **self._staged(),
        )

    def spawn_and_join(
        self,
        specs: list[SpawnSpec],
        *,
        mode: str,
        resume_point: str,
        saved_process_state: dict | None = None,
    ) -> ProcessResult:
        """Spawn ``specs`` as children and suspend until ``mode`` are done."""
        if mode not in ("all", "any"):
            raise ValueError(f"join mode must be 'all' or 'any', got {mode!r}")
        return ProcessResult(
            status=ProcessStatus.SUSPENDED,
            state_changes=list(self.state.changes),
            spawned_processes=list(specs),
            join=JoinRequest(
                mode=mode,
                resume_point=resume_point,
                saved_process_state=dict(saved_process_state or {}),
            ),
            **self._staged(),
        )

    def retry(self, error: object, *, delay: float | None = None) -> ProcessResult:
        """Return a *retryable* failure result.

        No world-changing effect is applied — but the attempt journals staged on
        this context are kept, so "we called the backend and it timed out"
        stays visible after the rollback (Invariant 26 / spec §69).
        """
        return ProcessResult(
            status=ProcessStatus.FAILED,
            output={"error": str(error)},
            retryable=True,
            retry_delay=delay,
            **self._staged_journals(),
        )

    def fail(
        self,
        error: object,
        *,
        emitted_events: list[Event] | None = None,
    ) -> ProcessResult:
        """Return a non-retryable FAILED result (attempt journals are kept)."""
        return ProcessResult(
            status=ProcessStatus.FAILED,
            output={"error": str(error)},
            emitted_events=list(emitted_events or []),
            **self._staged_journals(),
        )


# A process handler: async callable taking a ProcessContext, returning a result.
Handler = Callable[[ProcessContext], Awaitable[ProcessResult]]


class HandlerRegistry:
    """Maps handler names (as stored on definitions) to callables.

    Handlers are code, not data, so they are re-registered on every runtime
    construction.  Definitions in SQLite reference a handler by name.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        """Register ``handler`` under ``name`` (overwrites any existing)."""
        self._handlers[name] = handler

    def get(self, name: str) -> Handler:
        """Return the handler registered under ``name``.

        Raises:
            KeyError: If no handler is registered under that name.
        """
        try:
            return self._handlers[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"no handler registered for {name!r}") from exc

    def __contains__(self, name: object) -> bool:
        return name in self._handlers
