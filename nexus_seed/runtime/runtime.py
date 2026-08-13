"""Runtime — the NEXUS SEED event-driven engine.

The Runtime is *mechanism only*.  It holds no domain knowledge and makes no
"AI" decisions; all meaning lives in process handlers.  Its responsibilities:

    store events, route events, create processes, run processes, suspend
    processes, persist/match continuations, resume processes, persist state.

All durable state lives in SQLite, so a Runtime can be discarded and rebuilt
from the same database with no loss of process state.

Phase 2A makes the runtime *durable*:

* activations commit atomically (see :mod:`.executor`);
* duplicate events are ignored (idempotent intake);
* :meth:`recover` returns crash-orphaned RUNNING processes to RUNNABLE;
* :meth:`tick` fires due timers and promotes due retries;
* processes can spawn children and join on them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..core.event import Event
from ..core.process import (
    Handler,
    HandlerRegistry,
    ProcessDefinition,
    ProcessInstance,
    ProcessStatus,
)
from ..storage.activation_store import ActivationStore
from ..storage.continuation_store import ContinuationStore
from ..storage.database import Database
from ..storage.event_store import EventStore
from ..storage.join_store import JoinStore
from ..storage.observation_store import ObservationStore
from ..storage.process_store import ProcessStore
from ..storage.state_delta_store import StateDeltaStore
from ..storage.state_store import StateStore
from ..storage.timer_store import TimerStore
from ..storage.work_requirement_store import WorkRequirementStore
from ..storage.context_snapshot_store import ContextSnapshotStore
from ..storage.proposal_store import ProposalStore
from ..storage.llm_invocation_store import LLMInvocationStore
from ..storage.action_proposal_store import ActionProposalStore
from ..storage.action_execution_store import ActionExecutionStore
from ..storage.action_decision_store import ActionDecisionStore
from ..storage.ingress_receipt_store import IngressReceiptStore
from ..storage.adapter_checkpoint_store import AdapterCheckpointStore
from ..context.compiler import ContextCompiler
from ..context.models import ContextSnapshot
from ..backends.base import ExecutionBackend, LLMInvocation
from ..intelligence.proposal import InterpretationProposal
from ..actions.models import (
    ActionDecisionRecord,
    ActionExecution,
    ActionProposal,
    ActionProposalStatus,
)
from ..actions.trace import ActionTrace, get_action_trace
from ..ingress.models import AdapterCheckpoint, IngressReceipt
from ..ingress.trace import (
    IngressTrace,
    get_ingress_trace,
    get_ingress_trace_by_source_key,
)
from ..core.state import StateEntry, StateHistoryEntry
from ..world.provenance import Provenance, get_state_provenance
from ..work.trace import WorkTrace, get_work_trace
from ..work.work_requirement import WorkRequirement, WorkStatus
from .clock import Clock
from .services import RuntimeServices
from .continuation_resolver import ContinuationResolver
from .executor import Executor
from .join_coordinator import JoinCoordinator
from .router import Router
from .scheduler import Scheduler

logger = logging.getLogger("nexus_seed.runtime")


class Runtime:
    """Wires together stores, router, scheduler, executor and coordinators.

    Handlers are code and must be (re)registered on every construction via
    :meth:`register_process`; definitions and all other state are loaded from
    the SQLite database at ``db_path``.  Construction runs a crash-recovery
    sweep automatically.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        registry: HandlerRegistry | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.db = Database(db_path)
        self.clock = clock or Clock()

        self.event_store = EventStore(self.db)
        self.process_store = ProcessStore(self.db)
        self.continuation_store = ContinuationStore(self.db)
        self.state_store = StateStore(self.db)
        self.timer_store = TimerStore(self.db)
        self.join_store = JoinStore(self.db)
        self.activation_store = ActivationStore(self.db)
        self.observation_store = ObservationStore(self.db)
        self.state_delta_store = StateDeltaStore(self.db)
        self.work_requirement_store = WorkRequirementStore(self.db)
        self.context_snapshot_store = ContextSnapshotStore(self.db)
        self.proposal_store = ProposalStore(self.db)
        self.llm_invocation_store = LLMInvocationStore(self.db)
        self.action_proposal_store = ActionProposalStore(self.db)
        self.action_execution_store = ActionExecutionStore(self.db)
        self.action_decision_store = ActionDecisionStore(self.db)
        self.ingress_receipt_store = IngressReceiptStore(self.db)
        self.adapter_checkpoint_store = AdapterCheckpointStore(self.db)
        self.backends: dict[str, ExecutionBackend] = {}

        self.registry = registry or HandlerRegistry()
        self.resolver = ContinuationResolver(self.continuation_store, self.process_store)
        self.router = Router(self.process_store, self.resolver)
        self.scheduler = Scheduler(self.process_store)
        self.join_coordinator = JoinCoordinator(self.join_store, self.process_store)
        self.services = RuntimeServices(
            process_store=self.process_store,
            work_requirement_store=self.work_requirement_store,
            state_store=self.state_store,
            proposal_store=self.proposal_store,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
        )
        self.context_compiler = ContextCompiler(
            process_store=self.process_store,
            event_store=self.event_store,
            state_store=self.state_store,
            observation_store=self.observation_store,
            state_delta_store=self.state_delta_store,
            work_requirement_store=self.work_requirement_store,
            continuation_store=self.continuation_store,
        )
        self.executor = Executor(
            self.db,
            self.registry,
            self.process_store,
            self.continuation_store,
            self.state_store,
            self.event_store,
            self.timer_store,
            self.join_store,
            self.activation_store,
            self.observation_store,
            self.state_delta_store,
            self.work_requirement_store,
            self.clock,
            self.context_compiler,
            self.context_snapshot_store,
            self.proposal_store,
            self.llm_invocation_store,
            self.backends,
            self.services,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
            action_decision_store=self.action_decision_store,
        )

        self.recover()

    # --- registration ------------------------------------------------------

    def register_process(
        self, definition: ProcessDefinition, handler: Handler
    ) -> None:
        """Persist ``definition`` and bind its ``handler`` callable in memory."""
        self.process_store.upsert_definition(definition)
        self.registry.register(definition.handler, handler)
        logger.info("registered process %s v%s", definition.name, definition.version)

    def register_backend(self, name: str, backend: ExecutionBackend) -> None:
        """Register an execution backend under ``name`` (e.g. ``"llm"``).

        Handlers reach it via ``ctx.backends[name]``.  Registering a different
        implementation under the same name swaps the backend transparently.
        """
        self.backends[name] = backend
        logger.info("registered backend %s (%s)", name, type(backend).__name__)

    # --- intelligence queries ---------------------------------------------

    def get_proposal(self, proposal_id) -> InterpretationProposal | None:
        """Return a stored interpretation proposal by id."""
        return self.proposal_store.get(proposal_id)

    def get_proposals(self) -> list[InterpretationProposal]:
        """Return all stored interpretation proposals."""
        return self.proposal_store.all()

    def get_llm_invocation(self, invocation_id) -> LLMInvocation | None:
        """Return a stored LLM invocation record by id."""
        return self.llm_invocation_store.get(invocation_id)

    def get_llm_invocations(self, instance_id) -> list[LLMInvocation]:
        """Return every backend call an instance made (successes and failures)."""
        return self.llm_invocation_store.for_instance(instance_id)

    # --- action queries ----------------------------------------------------

    def get_action_proposal(self, proposal_id) -> ActionProposal | None:
        """Return a stored action proposal by id."""
        return self.action_proposal_store.get(proposal_id)

    def get_action_proposals(
        self, status: ActionProposalStatus | str | None = None
    ) -> list[ActionProposal]:
        """Return all action proposals, optionally filtered by ``status``."""
        if status is None:
            return self.action_proposal_store.all()
        return self.action_proposal_store.by_status(status)

    def get_action_executions(self, proposal_id) -> list[ActionExecution]:
        """Return every execution attempt made for a proposal, oldest first."""
        return self.action_execution_store.for_proposal(proposal_id)

    def get_action_decisions(self, proposal_id) -> list[ActionDecisionRecord]:
        """Return the authorization decisions recorded for a proposal."""
        return self.action_decision_store.for_proposal(proposal_id)

    def get_action_trace(self, proposal_id) -> ActionTrace | None:
        """Trace an action back through work, delta and observation to its event."""
        return get_action_trace(
            proposal_id,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
            action_decision_store=self.action_decision_store,
            process_store=self.process_store,
            work_requirement_store=self.work_requirement_store,
            state_delta_store=self.state_delta_store,
            observation_store=self.observation_store,
            event_store=self.event_store,
            context_snapshot_store=self.context_snapshot_store,
        )

    # --- ingress queries ---------------------------------------------------

    def get_ingress_receipt(self, receipt_id) -> IngressReceipt | None:
        """Return a stored ingress receipt by id."""
        return self.ingress_receipt_store.get(receipt_id)

    def get_ingress_receipts(self, adapter_id: str | None = None) -> list[IngressReceipt]:
        """Return ingress receipts, optionally only those from one adapter."""
        if adapter_id is None:
            return self.ingress_receipt_store.all()
        return self.ingress_receipt_store.for_adapter(adapter_id)

    def get_ingress_receipt_for_event(self, event_id) -> IngressReceipt | None:
        """Return the receipt that produced ``event_id`` (``None`` if internal)."""
        return self.ingress_receipt_store.for_event(event_id)

    def get_adapter_checkpoint(self, adapter_id: str, stream_key: str) -> AdapterCheckpoint | None:
        """Return one adapter's observation position for a stream."""
        return self.adapter_checkpoint_store.get(adapter_id, stream_key)

    def get_adapter_checkpoints(self, adapter_id: str) -> list[AdapterCheckpoint]:
        """Return every stream checkpoint held by ``adapter_id``."""
        return self.adapter_checkpoint_store.for_adapter(adapter_id)

    def get_ingress_trace(self, event_id) -> IngressTrace | None:
        """Trace an ingested event forward to everything it caused."""
        return get_ingress_trace(event_id, **self._ingress_trace_stores())

    def get_ingress_trace_by_source_key(
        self, adapter_id: str, source_event_key: str
    ) -> IngressTrace | None:
        """Trace an external identity forward, without knowing any NEXUS id."""
        return get_ingress_trace_by_source_key(
            adapter_id, source_event_key, **self._ingress_trace_stores()
        )

    def _ingress_trace_stores(self) -> dict:
        return {
            "ingress_receipt_store": self.ingress_receipt_store,
            "event_store": self.event_store,
            "observation_store": self.observation_store,
            "state_delta_store": self.state_delta_store,
            "work_requirement_store": self.work_requirement_store,
            "action_proposal_store": self.action_proposal_store,
        }

    # --- world-state queries ----------------------------------------------

    def get_current_state(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the current fact for ``entity.attribute`` (with provenance ids)."""
        return self.state_store.get_current(entity, attribute)

    def get_state_history(
        self, entity: str, attribute: str
    ) -> list[StateHistoryEntry]:
        """Return every version of ``entity.attribute``, oldest first."""
        return self.state_store.get_history(entity, attribute)

    def get_state_at_version(
        self, entity: str, attribute: str, version: int
    ) -> StateHistoryEntry | None:
        """Return a specific historical version of ``entity.attribute``."""
        return self.state_store.get_state_at_version(entity, attribute, version)

    def get_state_provenance(self, entity: str, attribute: str) -> Provenance | None:
        """Walk ``entity.attribute`` back to the raw event that produced it."""
        return get_state_provenance(
            entity,
            attribute,
            state_store=self.state_store,
            state_delta_store=self.state_delta_store,
            observation_store=self.observation_store,
            event_store=self.event_store,
        )

    def rebuild_current_state(self) -> int:
        """Rebuild the ``current`` projection from history; return facts rebuilt."""
        return self.state_store.rebuild_current_state()

    # --- work-intelligence queries ----------------------------------------

    def get_work_requirement(self, requirement_id) -> WorkRequirement | None:
        """Return a work requirement by id."""
        return self.work_requirement_store.get(requirement_id)

    def get_work_requirements(
        self, status: WorkStatus | str | None = None
    ) -> list[WorkRequirement]:
        """Return all work requirements, optionally filtered by ``status``."""
        if status is None:
            return self.work_requirement_store.all()
        return self.work_requirement_store.by_status(status)

    def get_work_trace(self, requirement_id) -> WorkTrace | None:
        """Trace a work requirement back to the raw event that caused it."""
        return get_work_trace(
            requirement_id,
            work_requirement_store=self.work_requirement_store,
            process_store=self.process_store,
            state_delta_store=self.state_delta_store,
            observation_store=self.observation_store,
            event_store=self.event_store,
        )

    # --- context (audit) ---------------------------------------------------

    def get_context_snapshots(self, instance_id) -> list[ContextSnapshot]:
        """Return all compiled-context audit snapshots for an instance."""
        return self.context_snapshot_store.for_instance(instance_id)

    def get_latest_context_snapshot(self, instance_id) -> ContextSnapshot | None:
        """Return the most recent context snapshot for an instance."""
        return self.context_snapshot_store.latest_for_instance(instance_id)

    # --- event intake ------------------------------------------------------

    async def submit_event(self, event: Event) -> list[Event]:
        """Ingest an external event and run the system until it goes idle.

        Idempotent: an event whose id was already ingested is ignored.  Returns
        every event produced by processes while draining (the submitted event
        itself is not included).
        """
        if self.event_store.get(event.id) is not None:
            logger.info("event %s already ingested; skipping (idempotent)", event.id)
            return []
        self.event_store.append(event)
        return await self.deliver_event(event)

    async def deliver_event(self, event: Event) -> list[Event]:
        """Route an **already-persisted** event and drain the system.

        The half of :meth:`submit_event` after the append.  Phase 3D's ingress
        boundary persists the event itself (inside the same transaction as its
        receipt), then calls this — so the event is stored exactly once but
        still runs through the ordinary router and scheduler.
        """
        self.router.route(event)
        return await self._drain()

    async def run_pending(self) -> list[Event]:
        """Drain any already-RUNNABLE processes (e.g. after :meth:`recover`)."""
        return await self._drain()

    async def tick(self) -> list[Event]:
        """Fire due timers, promote due retries, then drain.

        Time comes from the runtime's :class:`Clock`, so tests can drive this
        deterministically with a :class:`~nexus_seed.runtime.clock.ManualClock`.
        """
        now = self.clock.now()
        fired_events: list[Event] = []
        for timer in self.timer_store.due(now):
            self.timer_store.mark_fired(timer.id)
            fired = Event(
                type=timer.event_type, source="runtime.timer", payload=timer.payload
            )
            self.event_store.append(fired)
            self.router.route(fired)
            fired_events.append(fired)
            logger.info("timer %s fired -> %s", timer.id, timer.event_type)

        for instance in self.process_store.due_retries(now.isoformat()):
            instance.status = ProcessStatus.RUNNABLE
            instance.next_retry_at = None
            instance.updated_at = now
            self.process_store.save_instance(instance)
            logger.info("retry due: instance %s -> RUNNABLE", instance.id)

        return fired_events + await self._drain()

    # --- internals ---------------------------------------------------------

    async def _drain(self) -> list[Event]:
        """Run RUNNABLE processes until none remain, routing produced events."""
        produced: list[Event] = []
        while True:
            instance = self.scheduler.next_runnable()
            if instance is None:
                break
            result = await self.executor.execute(instance)

            for emitted in result.emitted_events:
                produced.append(emitted)
                self.router.route(emitted)

            # instance.status was updated in place by the executor.
            for join_event in self.join_coordinator.on_instance_finished(instance):
                self.event_store.append(join_event)
                produced.append(join_event)
                self.router.route(join_event)
        return produced

    def recover(self) -> list[ProcessInstance]:
        """Return crash-orphaned RUNNING instances to RUNNABLE.

        A committed activation always advances an instance out of RUNNING as
        part of its transaction, so any instance still RUNNING at startup was
        interrupted before committing and is safe to re-run.
        """
        recovered = []
        for instance in self.process_store.instances_by_status(ProcessStatus.RUNNING):
            instance.status = ProcessStatus.RUNNABLE
            instance.updated_at = self.clock.now()
            self.process_store.save_instance(instance)
            logger.warning("recovered instance %s: RUNNING -> RUNNABLE", instance.id)
            recovered.append(instance)
        return recovered

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
