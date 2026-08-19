"""RuntimeServices — a read-only facade handlers use for cross-cutting reads.

Process handlers stay decoupled from storage internals: they *read* through
``ctx.services`` and *write* only by returning a ProcessResult.  This facade
exposes reads (existing processes, work requirements, current state) with no
domain logic of its own, so the Runtime remains pure mechanism.
"""

from __future__ import annotations

import uuid

from ..core.process import ProcessDefinition, ProcessInstance, ProcessStatus
from ..core.state import StateEntry
from ..storage.process_store import ProcessStore
from ..storage.state_store import StateStore
from ..storage.work_requirement_store import WorkRequirementStore
from ..work.work_requirement import WorkRequirement

#: Statuses that count as "work is still being done".
ACTIVE_STATUSES = (
    ProcessStatus.RUNNABLE,
    ProcessStatus.RUNNING,
    ProcessStatus.SUSPENDED,
    ProcessStatus.RETRY_WAIT,
)


class RuntimeServices:
    """Read-only queries available to process handlers via ``ctx.services``."""

    def __init__(
        self,
        *,
        process_store: ProcessStore,
        work_requirement_store: WorkRequirementStore,
        state_store: StateStore,
        proposal_store=None,
        action_proposal_store=None,
        action_execution_store=None,
        resource_service=None,
        extractor_registry=None,
        ingress_receipt_store=None,
        capability_registry=None,
        capability_matcher=None,
        capability_store=None,
        plan_store=None,
        composition_planner=None,
        plan_validator=None,
        decision_store=None,
        plan_evaluator=None,
        plan_selector=None,
        selection_validator=None,
        selection_policy=None,
        extension_store=None,
        acquisition_analyzer=None,
        extension_validator=None,
        construction_store=None,
        construction_planner=None,
        construction_validator=None,
        workspace_manager=None,
        installation_store=None,
        installation_planner=None,
        installation_validator=None,
        installation_policy=None,
        installation_manager=None,
        autonomy_store=None,
        autonomy_policy=None,
        autonomy_budget=None,
        provider_registry=None,
        continuation_store=None,
        control_store=None,
        runtime=None,
    ) -> None:
        self._construction_store = construction_store
        self._construction_planner = construction_planner
        self._construction_validator = construction_validator
        self._workspace_manager = workspace_manager
        self._installation_store = installation_store
        self._installation_planner = installation_planner
        self._installation_validator = installation_validator
        self._installation_policy = installation_policy
        self._installation_manager = installation_manager
        self._autonomy_store = autonomy_store
        self._autonomy_policy = autonomy_policy
        self._autonomy_budget = autonomy_budget
        self._providers = provider_registry
        self._continuation_store = continuation_store
        self._control_store = control_store
        self._extension_store = extension_store
        self._acquisition_analyzer = acquisition_analyzer
        self._extension_validator = extension_validator
        self._capabilities = capability_registry
        self._capability_matcher = capability_matcher
        self._capability_store = capability_store
        self._plans = plan_store
        self._composition_planner = composition_planner
        self._plan_validator = plan_validator
        self._decision_store = decision_store
        self._plan_evaluator = plan_evaluator
        self._plan_selector = plan_selector
        self._selection_validator = selection_validator
        self._selection_policy = selection_policy
        #: The Runtime itself, for the few decision settings a handler needs to
        #: read (which selector to ask, how many replans are allowed).  Those
        #: are deployment configuration, not Runtime mechanism (spec §76) — the
        #: Runtime holds them, the handlers decide with them.
        self._runtime = runtime
        self._resources = resource_service
        self._extractors = extractor_registry
        self._ingress_receipt_store = ingress_receipt_store
        self._process_store = process_store
        self._work_requirement_store = work_requirement_store
        self._state_store = state_store
        self._proposal_store = proposal_store
        self._action_proposal_store = action_proposal_store
        self._action_execution_store = action_execution_store

    # --- work requirements -------------------------------------------------

    def get_work_requirement(self, requirement_id: uuid.UUID) -> WorkRequirement | None:
        """Return a work requirement by id."""
        return self._work_requirement_store.get(requirement_id)

    def get_work_requirement_by_key(self, work_key: str) -> WorkRequirement | None:
        """Return a work requirement by its logical ``work_key``."""
        return self._work_requirement_store.get_by_work_key(work_key)

    def get_work_for_goal(self, goal_id) -> list[WorkRequirement]:
        """Return every WorkRequirement generated for a Goal."""
        return self._work_requirement_store.for_goal(goal_id)

    def get_goal(self, goal_id):
        """Return one durable Goal for event-driven evaluation."""
        return self._control_store.get_goal(goal_id) if self._control_store else None

    def get_active_goals(self) -> list:
        """Return ACTIVE goals for state/work-triggered reevaluation."""
        if self._control_store is None:
            return []
        return self._control_store.goals("ACTIVE")

    def get_active_pursuits(self) -> list:
        """Return what is currently being pursued, whatever supplies it."""
        return self._runtime.active_pursuits() if self._runtime is not None else []

    def get_pursuit(self, pursuit_id):
        """Return one pursuit by id, live or not, or ``None`` when unknown."""
        return self._runtime.get_pursuit(pursuit_id) if self._runtime is not None else None

    def get_control_store(self):
        """Read-side access to Goal records and command provenance."""
        return self._control_store

    def get_project_situation(self, project_id: str):
        """Compile one read-only ProjectSituation for a Process/LLM handler."""

        if self._runtime is None:
            return None
        from ..projects.projections import get_project_situation

        return get_project_situation(self._runtime, project_id)

    def get_project_summaries(self):
        """Compile compact project summaries for a Process/LLM handler."""

        if self._runtime is None:
            return []
        from ..projects.projections import get_project_summaries

        return get_project_summaries(self._runtime)

    # --- processes ---------------------------------------------------------

    def get_process_instance(self, instance_id: uuid.UUID) -> ProcessInstance | None:
        """Return a process instance by id (used to attribute a proposal)."""
        return self._process_store.get_instance(instance_id)

    def get_definition(self, name: str, version: str) -> ProcessDefinition | None:
        """Return a ProcessDefinition — the carrier of granted permissions."""
        return self._process_store.get_definition(name, version)

    def get_provider_registry(self):
        """Return the read-side operational provider self-model."""
        return self._providers

    def find_processes_by_work_key(self, work_key: str) -> list[ProcessInstance]:
        """Return every process (any status) fulfilling ``work_key``."""
        return self._process_store.find_by_work_key(work_key)

    def active_processes_for_work_key(self, work_key: str) -> list[ProcessInstance]:
        """Return processes fulfilling ``work_key`` that are still active."""
        return [
            p
            for p in self._process_store.find_by_work_key(work_key)
            if p.status in ACTIVE_STATUSES
        ]

    def completed_processes_for_work_key(self, work_key: str) -> list[ProcessInstance]:
        """Return completed processes fulfilling ``work_key``."""
        return [
            p
            for p in self._process_store.find_by_work_key(work_key)
            if p.status is ProcessStatus.COMPLETED
        ]

    # --- proposals ---------------------------------------------------------

    def get_proposal(self, proposal_id: uuid.UUID):
        """Return a stored interpretation proposal by id (or ``None``)."""
        if self._proposal_store is None:
            return None
        return self._proposal_store.get(proposal_id)

    # --- actions -----------------------------------------------------------

    def get_action_proposal(self, proposal_id: uuid.UUID):
        """Return a stored action proposal by id (or ``None``)."""
        if self._action_proposal_store is None:
            return None
        return self._action_proposal_store.get(proposal_id)

    def get_action_executions(self, proposal_id: uuid.UUID) -> list:
        """Return every execution attempt recorded for a proposal."""
        if self._action_execution_store is None:
            return []
        return self._action_execution_store.for_proposal(proposal_id)

    def find_succeeded_action_execution(self, idempotency_key: str | None):
        """Return a SUCCEEDED execution for ``idempotency_key``, if one exists.

        The idempotency guard: a hit means the external effect already happened
        and the backend must not be called again (Invariant 27).
        """
        if self._action_execution_store is None:
            return None
        return self._action_execution_store.succeeded_for_key(idempotency_key)

    # --- capabilities ------------------------------------------------------

    def get_capability_registry(self):
        """Return the read side of the capability registry."""
        return self._capabilities

    def get_capability_matcher(self):
        """Return the deterministic capability matcher."""
        return self._capability_matcher

    def get_all_definitions(self) -> list[ProcessDefinition]:
        """Return every registered ProcessDefinition (matching candidates)."""
        return self._process_store.all_definitions()

    def get_capability_matches(self, work_requirement_id: uuid.UUID) -> list:
        """Return the matching attempts recorded for a work requirement."""
        if self._capability_store is None:
            return []
        return self._capability_store.matches_for(work_requirement_id)

    def get_work_by_status(self, status) -> list[WorkRequirement]:
        """Return work requirements in a given status (used by reconciliation)."""
        return self._work_requirement_store.by_status(status)

    # --- planning ----------------------------------------------------------

    def get_plan_store(self):
        """Return the plan store (reads; writes stay declarative)."""
        return self._plans

    def get_composition_planner(self):
        """Return the deterministic composition planner."""
        return self._composition_planner

    def get_plan_validator(self):
        """Return the plan validator."""
        return self._plan_validator

    def get_active_plan(self, work_requirement_id: uuid.UUID):
        """Return the plan currently pursuing a requirement, if any."""
        if self._plans is None:
            return None
        return self._plans.active_for_work(work_requirement_id)

    # --- decision (Phase 4C) -----------------------------------------------

    def get_decision_store(self):
        """Return the decision store (evaluations, proposals, selections)."""
        return self._decision_store

    def get_plan_evaluator(self):
        """Return the deterministic plan evaluator."""
        return self._plan_evaluator

    def get_plan_selector(self):
        """Return the deterministic selector — always available (Invariant 77)."""
        return self._plan_selector

    def get_llm_plan_selector(self):
        """Return the optional LLM selector, or ``None`` if none is configured."""
        return getattr(self._runtime, "llm_plan_selector", None)

    def get_selection_validator(self):
        """Return the validator a chosen plan must pass before it runs."""
        return self._selection_validator

    def get_selection_policy(self):
        """Return the confidence policy for acting on a proposed selection."""
        return self._selection_policy

    def get_default_max_replans(self) -> int:
        """How many replans a need gets when its own requirement does not say."""
        return getattr(self._runtime, "default_max_replans", 3)

    def get_plan_candidates(self, work_requirement_id: uuid.UUID) -> list:
        """Candidate plans for a need that could still be chosen."""
        if self._plans is None:
            return []
        return self._plans.candidates_for_work(work_requirement_id)

    def get_failed_plan_fingerprints(self, work_requirement_id: uuid.UUID) -> list[str]:
        """Plan shapes that have already failed terminally for this need."""
        if self._plans is None:
            return []
        return self._plans.failed_fingerprints(work_requirement_id)

    # --- self-extension (Phase 5A) -----------------------------------------

    def get_extension_store(self):
        """Return the store of gaps, extension proposals and their decisions."""
        return self._extension_store

    def get_acquisition_analyzer(self):
        """Return the deterministic acquisition analyzer (never an LLM)."""
        return self._acquisition_analyzer

    def get_extension_validator(self):
        """Return the validator an extension proposal must pass."""
        return self._extension_validator

    def get_llm_extension_proposer(self):
        """Return the optional LLM proposer, or ``None`` if none is configured."""
        return getattr(self._runtime, "llm_extension_proposer", None)

    def get_acquisition_environment(self):
        """What the analyzer may look at: definitions, backends, extractors, …"""
        if self._runtime is None:
            return None
        return self._runtime.acquisition_environment()

    def get_capability_gap(self, gap_id: uuid.UUID):
        """Return one recorded deficiency (or ``None``)."""
        if self._extension_store is None:
            return None
        return self._extension_store.get_gap(gap_id)

    def find_capability_gap(self, work_requirement_id: uuid.UUID, missing_key: str):
        """The gap for this need and this exact missing set (spec §11)."""
        if self._extension_store is None:
            return None
        return self._extension_store.find_gap(work_requirement_id, missing_key)

    def get_open_capability_gaps(self) -> list:
        """Every deficiency that still stands."""
        if self._extension_store is None:
            return []
        return self._extension_store.open_gaps()

    def get_extension_proposal(self, proposal_id: uuid.UUID):
        """Return one extension proposal (or ``None``)."""
        if self._extension_store is None:
            return None
        return self._extension_store.get_proposal(proposal_id)

    def get_extension_proposals_for_gap(self, gap_id: uuid.UUID) -> list:
        """Every proposal made about a gap, oldest first."""
        if self._extension_store is None:
            return []
        return self._extension_store.proposals_for_gap(gap_id)

    def find_extension_proposal_by_fingerprint(self, fingerprint: str):
        """The proposal with this content, if one was already made (spec §68)."""
        if self._extension_store is None:
            return None
        return self._extension_store.get_proposal_by_fingerprint(fingerprint)

    # --- sandboxed construction (Phase 5B) --------------------------------

    def get_construction_store(self):
        """Return the read side of durable construction records."""
        return self._construction_store

    def get_construction_planner(self):
        """Return the deterministic construction template builder."""
        return self._construction_planner

    def get_construction_validator(self):
        """Return the validator guarding workspace creation."""
        return self._construction_validator

    def get_workspace_manager(self):
        """Return the dedicated-root workspace manager."""
        return self._workspace_manager

    def get_llm_construction_generator(self):
        """Return the optional sandbox-artifact generator."""
        return getattr(self._runtime, "llm_construction_generator", None)

    def get_construction_plan(self, plan_id):
        """Read one ConstructionPlan by id."""
        return self._construction_store.get_plan(plan_id) if self._construction_store else None

    def get_active_construction_plan(self, proposal_id):
        """Read the nonterminal plan for an ExtensionProposal, if any."""
        return self._construction_store.active_for_proposal(proposal_id) if self._construction_store else None

    def get_construction_plans_for_proposal(self, proposal_id) -> list:
        """Read every durable attempt for an ExtensionProposal."""
        return self._construction_store.plans_for_proposal(proposal_id) if self._construction_store else []

    def get_sandbox_workspace_for_plan(self, plan_id):
        """Read the workspace belonging to a construction plan."""
        return self._construction_store.get_workspace_for_plan(plan_id) if self._construction_store else None

    def get_construction_grant_for_plan(self, plan_id):
        """Read the plan's construction-scoped grant."""
        return self._construction_store.get_grant_for_plan(plan_id) if self._construction_store else None

    def get_verification_checks_for_plan(self, plan_id):
        """Read all three layers of verification evidence."""
        return self._construction_store.checks_for_plan(plan_id) if self._construction_store else []

    def get_construction_result(self, plan_id):
        """Read a plan's terminal construction result."""
        return self._construction_store.result_for_plan(plan_id) if self._construction_store else None

    def get_construction_result_by_id(self, result_id):
        """Read a terminal construction result by its own id."""
        return self._construction_store.get_result(result_id) if self._construction_store else None

    # --- production installation (Phase 5C) ------------------------------

    def get_installation_store(self):
        return self._installation_store

    def get_installation_planner(self):
        return self._installation_planner

    def get_installation_validator(self):
        return self._installation_validator

    def get_installation_policy(self):
        return getattr(self._runtime, "installation_policy", self._installation_policy)

    def get_installation_manager(self):
        return self._installation_manager

    def get_installation_plan(self, plan_id):
        return self._installation_store.get_plan(plan_id) if self._installation_store else None

    def get_installation_plan_for_result(self, result_id):
        return self._installation_store.for_construction_result(result_id) if self._installation_store else None

    def get_installation_grant_for_plan(self, plan_id):
        return self._installation_store.grant_for_plan(plan_id) if self._installation_store else None

    def get_installation_result(self, plan_id):
        return self._installation_store.result_for_plan(plan_id) if self._installation_store else None

    # --- autonomous capability acquisition (Phase 5D) --------------------

    def get_autonomy_store(self):
        """Return the read/query store used by the acquisition Process."""
        return self._autonomy_store

    def get_autonomy_policy(self):
        """Return deployment policy; handlers never mutate it."""
        return getattr(self._runtime, "autonomy_policy", self._autonomy_policy)

    def get_default_autonomy_budget(self):
        """Return the default immutable budget for newly opened sessions."""
        return getattr(self._runtime, "default_autonomy_budget", self._autonomy_budget)

    def get_acquisition_session(self, session_id):
        return self._autonomy_store.get_session(session_id) if self._autonomy_store else None

    def get_acquisition_trace(self, session_id):
        """Read what an acquisition already tried, for explaining a stop."""

        if self._runtime is None:
            return None
        return self._runtime.get_acquisition_trace(session_id)

    def get_capability_gaps_for_work(self, work_requirement_id) -> list:
        """Read the deficiencies recorded against one WorkRequirement."""

        if self._extension_store is None:
            return []
        return self._extension_store.gaps_for_work(work_requirement_id)

    def find_acquisition_by_key(self, acquisition_key):
        return self._autonomy_store.find_by_key(acquisition_key) if self._autonomy_store else None

    def find_acquisition_by_proposal(self, proposal_id):
        return self._autonomy_store.find_by_proposal(proposal_id) if self._autonomy_store else None

    def find_acquisition_by_construction_plan(self, plan_id):
        return self._autonomy_store.find_by_construction_plan(plan_id) if self._autonomy_store else None

    def find_acquisition_by_installation_plan(self, plan_id):
        return self._autonomy_store.find_by_installation_plan(plan_id) if self._autonomy_store else None

    def get_activation_for_definition(self, name, version):
        return self._installation_store.activation_for_definition(name, version) if self._installation_store else None

    def get_resource_store(self):
        """Read-only store access for exact immutable ResourceVersion identity."""
        return self._resources.store if self._resources is not None else None

    def find_extension_review_continuations(self, proposal_id) -> list:
        """Read-only lookup used to close superseded/cancelled reviews."""
        if self._continuation_store is None:
            return []
        target = str(proposal_id)
        return [
            c for c in self._continuation_store.all()
            if c.resume_point == "await_extension_review"
            and str(c.saved_process_state.get("extension_proposal_id")) == target
        ]

    def find_autonomy_review_continuations(self, session_id) -> list:
        """Find durable Phase 5D review waiters for cancellation cleanup."""
        if self._continuation_store is None:
            return []
        return [
            continuation
            for continuation in self._continuation_store.all()
            if continuation.resume_point == "await_autonomy_review"
            and str(continuation.waiting_for.get("acquisition_session_id"))
            == str(session_id)
        ]

    # --- resources ---------------------------------------------------------

    def get_resource_service(self):
        """Return the read-side :class:`ResourceService`.

        Handed out so a handler can ask *what would indexing this observation
        imply* — a pure read-and-compute.  The objects it returns are staged on
        the ProcessResult; nothing here writes.
        """
        return self._resources

    def get_extractors(self):
        """Return the extractor registry (or ``None`` if none is configured)."""
        return self._extractors

    def get_representation(self, representation_id: uuid.UUID):
        """Return a stored Representation by id (or ``None``)."""
        if self._resources is None:
            return None
        return self._resources.store.get_representation(representation_id)

    def get_ingress_receipt_for_event(self, event_id: uuid.UUID):
        """Return the ingress receipt an event came from (``None`` if internal)."""
        if self._ingress_receipt_store is None:
            return None
        return self._ingress_receipt_store.for_event(event_id)

    def get_resource(self, resource_id: uuid.UUID):
        """Return a Resource by id (or ``None``)."""
        return self._resources.get_resource(resource_id) if self._resources else None

    def get_resource_by_uri(self, uri: str):
        """Return a Resource by its URI (or ``None``)."""
        return self._resources.get_resource_by_uri(uri) if self._resources else None

    def get_current_resource_version(self, resource_id: uuid.UUID):
        """Return the newest version of a Resource (or ``None``)."""
        return self._resources.get_current_version(resource_id) if self._resources else None

    # --- state -------------------------------------------------------------

    def get_current_state(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the current fact for ``entity.attribute``."""
        return self._state_store.get_current(entity, attribute)

    def get_current_state_by_prefix(self, entity_prefix: str) -> list[StateEntry]:
        """Return current facts whose entity starts with an explicit prefix.

        This is the narrow read needed by Phase 6 projection Processes for
        dynamic ``intention:<id>`` entities.  It remains a read-only selective
        query; handlers still stage every write on their ProcessResult.
        """

        return [
            entry
            for entry in self._state_store.all_current()
            if entry.entity.startswith(entity_prefix)
        ]
