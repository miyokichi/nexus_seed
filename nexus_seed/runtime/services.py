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
    ) -> None:
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

    # --- state -------------------------------------------------------------

    def get_current_state(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the current fact for ``entity.attribute``."""
        return self._state_store.get_current(entity, attribute)
