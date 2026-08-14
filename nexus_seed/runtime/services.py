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
        runtime=None,
    ) -> None:
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

    # --- processes ---------------------------------------------------------

    def get_process_instance(self, instance_id: uuid.UUID) -> ProcessInstance | None:
        """Return a process instance by id (used to attribute a proposal)."""
        return self._process_store.get_instance(instance_id)

    def get_definition(self, name: str, version: str) -> ProcessDefinition | None:
        """Return a ProcessDefinition — the carrier of granted permissions."""
        return self._process_store.get_definition(name, version)

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
