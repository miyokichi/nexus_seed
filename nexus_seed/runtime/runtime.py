"""Durable mechanism for Events, Processes, Continuations and Resources."""

from __future__ import annotations

import logging
from pathlib import Path

from ..backends.base import ExecutionBackend
from ..context.compiler import ContextCompiler
from ..context.models import ContextSnapshot
from ..core.event import Event
from ..core.process import (
    Handler,
    HandlerRegistry,
    ProcessDefinition,
    ProcessInstance,
    ProcessStatus,
)
from ..core.state import StateEntry, StateHistoryEntry
from ..delivery.dispatcher import DurableEventDispatcher
from ..delivery.models import EventDelivery, EventDeliveryStatus
from ..ingress.models import AdapterCheckpoint, IngressReceipt
from ..ingress.service import AdapterRegistry, IngressService
from ..ingress.trace import IngressTrace, get_ingress_trace, get_ingress_trace_by_source_key
from ..resources.extractors import default_registry
from ..resources.models import Resource, ResourceRepresentation, ResourceVersion
from ..resources.scope import ResourceScope
from ..resources.service import ResourceService
from ..resources.trace import (
    RepresentationTrace,
    ResourceTrace,
    get_representation_trace,
    get_resource_trace,
)
from ..storage.activation_store import ActivationStore
from ..storage.adapter_checkpoint_store import AdapterCheckpointStore
from ..storage.chat_store import ProjectChatStore
from ..storage.context_snapshot_store import ContextSnapshotStore
from ..storage.continuation_store import ContinuationStore
from ..storage.database import Database
from ..storage.event_delivery_store import EventDeliveryStore
from ..storage.event_store import EventStore
from ..storage.ingress_receipt_store import IngressReceiptStore
from ..storage.join_store import JoinStore
from ..storage.knowledge_store import KnowledgeStore
from ..storage.observation_source_store import ObservationSourceStore
from ..storage.process_store import ProcessStore
from ..storage.resource_store import ResourceStore
from ..storage.state_store import StateStore
from ..storage.timer_store import TimerStore
from .clock import Clock
from .continuation_resolver import ContinuationResolver
from .drain import UNLIMITED, DrainBudget, DrainResult
from .executor import Executor
from .join_coordinator import JoinCoordinator
from .router import Router
from .scheduler import Scheduler
from .services import RuntimeServices

logger = logging.getLogger("nexus_seed.runtime")


class Runtime:
    """Wire the durable runtime mechanisms around one SQLite database."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        registry: HandlerRegistry | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.db = Database(db_path)
        self.clock = clock or Clock()

        self.event_delivery_store = EventDeliveryStore(self.db)
        self.event_store = EventStore(self.db, self.event_delivery_store)
        self.process_store = ProcessStore(self.db)
        self.continuation_store = ContinuationStore(self.db)
        self.state_store = StateStore(self.db)
        self.timer_store = TimerStore(self.db)
        self.join_store = JoinStore(self.db)
        self.activation_store = ActivationStore(self.db)
        self.context_snapshot_store = ContextSnapshotStore(self.db)
        self.ingress_receipt_store = IngressReceiptStore(self.db)
        self.adapter_checkpoint_store = AdapterCheckpointStore(self.db)
        self.chat_store = ProjectChatStore(self.db)
        self.knowledge_store = KnowledgeStore(self.db)
        self.observation_source_store = ObservationSourceStore(self.db)
        self.resource_store = ResourceStore(self.db)

        self.backends: dict[str, ExecutionBackend] = {}
        self.adapters = AdapterRegistry()
        self.ingress = IngressService(self)
        self.resource_scope: ResourceScope | None = None
        self.resource_service = ResourceService(self.resource_store)
        self.extractors = default_registry()
        self.project_orchestrator = None
        self.knowledge_loop = None
        #: Optional meaning-model Knowledge backend, set by application
        #: composition when one is configured.  Runtime never queries it.
        self.semantic_knowledge = None
        self.observation_sources = None

        self.registry = registry or HandlerRegistry()
        self.resolver = ContinuationResolver(self.continuation_store, self.process_store)
        self.router = Router(self.process_store, self.resolver, self.registry)
        self.scheduler = Scheduler(self.process_store)
        self.join_coordinator = JoinCoordinator(self.join_store, self.process_store)
        self.services = RuntimeServices(self)
        self.context_compiler = ContextCompiler(
            process_store=self.process_store,
            event_store=self.event_store,
            state_store=self.state_store,
            continuation_store=self.continuation_store,
            resource_store=self.resource_store,
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
            self.clock,
            compiler=self.context_compiler,
            context_snapshot_store=self.context_snapshot_store,
            backends=self.backends,
            services=self.services,
            resource_store=self.resource_store,
            adapters=self.adapters,
            ingress=self.ingress,
        )
        self.default_drain_budget: DrainBudget = UNLIMITED
        self.last_drain: DrainResult | None = None
        self.dispatcher = DurableEventDispatcher(
            self.db,
            self.event_store,
            self.event_delivery_store,
            self.router,
            self.clock,
        )
        self.recover()

    # -- wiring ------------------------------------------------------------

    def register_process(self, definition: ProcessDefinition, handler: Handler) -> None:
        """Persist a definition and make its handler available in this process."""
        self.process_store.upsert_definition(definition)
        self.registry.register(definition.handler, handler)
        logger.info("registered process %s v%s", definition.name, definition.version)

    def get_definition(self, name: str, version: str) -> ProcessDefinition | None:
        """Return one registered definition."""
        return self.process_store.get_definition(name, version)

    def set_project_orchestrator(self, orchestrator) -> None:
        """Expose the Project Orchestrator to the one routing Process."""
        self.project_orchestrator = orchestrator
        self.executor.project_orchestrator = orchestrator

    def register_backend(self, name: str, backend: ExecutionBackend) -> None:
        """Register a swappable execution backend such as ``llm``."""
        self.backends[name] = backend
        logger.info("registered backend %s (%s)", name, type(backend).__name__)

    def register_adapter(self, adapter, *, bind: bool = True):
        """Register an ingress adapter and optionally bind its checkpoint service."""
        if bind and hasattr(adapter, "bind"):
            adapter.bind(self.ingress)
        return self.adapters.register(adapter)

    def set_resource_scope(self, scope: ResourceScope) -> ResourceScope:
        """Confine Resource reads to the given path scope."""
        self.resource_scope = scope
        self.resource_service.scope = scope
        return scope

    # -- delivery ----------------------------------------------------------

    def get_event_delivery(self, event_id) -> EventDelivery | None:
        """Return one Event delivery obligation."""
        return self.event_delivery_store.get(event_id)

    def get_event_deliveries(
        self, status: EventDeliveryStatus | str | None = None
    ) -> list[EventDelivery]:
        """Return delivery records, optionally filtered by status."""
        return (
            self.event_delivery_store.all()
            if status is None
            else self.event_delivery_store.by_status(status)
        )

    def get_pending_event_delivery_count(self) -> int:
        """Return how many persisted Events still need routing."""
        return self.event_delivery_store.count_pending()

    def get_failed_event_deliveries(self) -> list[EventDelivery]:
        """Return permanently failed delivery records."""
        return self.event_delivery_store.by_status(EventDeliveryStatus.FAILED)

    def get_delivery_health(self) -> dict:
        """Return an operational delivery queue snapshot."""
        return self.dispatcher.health()

    # -- ingress -----------------------------------------------------------

    def get_ingress_receipt(self, receipt_id) -> IngressReceipt | None:
        """Return one IngressReceipt."""
        return self.ingress_receipt_store.get(receipt_id)

    def get_ingress_receipts(self, adapter_id: str | None = None) -> list[IngressReceipt]:
        """Return all receipts or those belonging to one adapter."""
        return (
            self.ingress_receipt_store.all()
            if adapter_id is None
            else self.ingress_receipt_store.for_adapter(adapter_id)
        )

    def get_ingress_receipt_for_event(self, event_id) -> IngressReceipt | None:
        """Return the external receipt that produced an Event."""
        return self.ingress_receipt_store.for_event(event_id)

    def get_adapter_checkpoint(
        self, adapter_id: str, stream_key: str
    ) -> AdapterCheckpoint | None:
        """Return one adapter stream checkpoint."""
        return self.adapter_checkpoint_store.get(adapter_id, stream_key)

    def get_adapter_checkpoints(self, adapter_id: str) -> list[AdapterCheckpoint]:
        """Return all checkpoints belonging to an adapter."""
        return self.adapter_checkpoint_store.for_adapter(adapter_id)

    def get_ingress_trace(self, event_id) -> IngressTrace | None:
        """Trace one external Event to its durable Process activations."""
        return get_ingress_trace(
            event_id,
            ingress_receipt_store=self.ingress_receipt_store,
            event_store=self.event_store,
            process_store=self.process_store,
        )

    def get_ingress_trace_by_source_key(
        self, adapter_id: str, source_event_key: str
    ) -> IngressTrace | None:
        """Trace an external source identity without needing an Event id."""
        return get_ingress_trace_by_source_key(
            adapter_id,
            source_event_key,
            ingress_receipt_store=self.ingress_receipt_store,
            event_store=self.event_store,
            process_store=self.process_store,
        )

    # -- resources ---------------------------------------------------------

    def get_resource(self, resource_id) -> Resource | None:
        return self.resource_store.get_resource(resource_id)

    def get_resource_by_uri(self, uri: str) -> Resource | None:
        return self.resource_store.get_resource_by_uri(uri)

    def get_resources(self) -> list[Resource]:
        return self.resource_store.all_resources()

    def get_current_resource_version(self, resource_id) -> ResourceVersion | None:
        return self.resource_store.get_current_version(resource_id)

    def get_resource_versions(self, resource_id) -> list[ResourceVersion]:
        return self.resource_store.get_versions(resource_id)

    def get_resource_version(self, resource_id, version: int) -> ResourceVersion | None:
        return self.resource_store.get_version_number(resource_id, version)

    def get_representation(self, representation_id) -> ResourceRepresentation | None:
        return self.resource_store.get_representation(representation_id)

    def get_representations(self, resource_version_id) -> list[ResourceRepresentation]:
        return self.resource_store.list_representations(resource_version_id)

    def find_representation(
        self, resource_version_id, representation_type: str
    ) -> ResourceRepresentation | None:
        return self.resource_store.find_representation(
            resource_version_id, representation_type
        )

    def get_resource_trace(self, resource_version_id) -> ResourceTrace | None:
        return get_resource_trace(
            resource_version_id,
            resource_store=self.resource_store,
            event_store=self.event_store,
            ingress_receipt_store=self.ingress_receipt_store,
        )

    def get_representation_trace(self, representation_id) -> RepresentationTrace | None:
        return get_representation_trace(
            representation_id,
            resource_store=self.resource_store,
            process_store=self.process_store,
            event_store=self.event_store,
            ingress_receipt_store=self.ingress_receipt_store,
        )

    # -- state/context -----------------------------------------------------

    def get_current_state(self, entity: str, attribute: str) -> StateEntry | None:
        return self.state_store.get_current(entity, attribute)

    def get_state_history(self, entity: str, attribute: str) -> list[StateHistoryEntry]:
        return self.state_store.get_history(entity, attribute)

    def get_state_at_version(
        self, entity: str, attribute: str, version: int
    ) -> StateHistoryEntry | None:
        return self.state_store.get_state_at_version(entity, attribute, version)

    def rebuild_current_state(self) -> int:
        return self.state_store.rebuild_current_state()

    def get_context_snapshots(self, instance_id) -> list[ContextSnapshot]:
        return self.context_snapshot_store.for_instance(instance_id)

    def get_latest_context_snapshot(self, instance_id) -> ContextSnapshot | None:
        return self.context_snapshot_store.latest_for_instance(instance_id)

    # -- event loop --------------------------------------------------------

    async def submit_event(
        self, event: Event, budget: DrainBudget | None = None
    ) -> list[Event]:
        """Persist an Event idempotently and drive the Runtime."""
        if self.event_store.get(event.id) is not None:
            logger.info("event %s already ingested; skipping", event.id)
            return []
        self.event_store.append(event)
        return await self._drain(budget)

    async def drain(self, budget: DrainBudget | None = None) -> list[Event]:
        """Run one bounded or unbounded slice of pending work."""
        return await self._drain(budget)

    async def deliver_event(self, event: Event) -> list[Event]:
        """Wake delivery after an Event was persisted by another boundary."""
        return await self._drain()

    def dispatch_pending_events(self, limit: int | None = None) -> int:
        """Route persisted Events whose delivery obligation is outstanding."""
        return self.dispatcher.dispatch_pending(limit)

    async def run_pending(self, budget: DrainBudget | None = None) -> list[Event]:
        return await self._drain(budget)

    async def tick(self, budget: DrainBudget | None = None) -> list[Event]:
        """Fire due timers, promote due retries, and drain."""
        now = self.clock.now()
        fired_events: list[Event] = []
        for timer in self.timer_store.due(now):
            self.timer_store.mark_fired(timer.id)
            event = Event(timer.event_type, "runtime.timer", timer.payload)
            self.event_store.append(event)
            fired_events.append(event)
            logger.info("timer %s fired -> %s", timer.id, timer.event_type)
        for instance in self.process_store.due_retries(now.isoformat()):
            instance.status = ProcessStatus.RUNNABLE
            instance.next_retry_at = None
            instance.updated_at = now
            self.process_store.save_instance(instance)
        return fired_events + await self._drain(budget)

    async def _drain(self, budget: DrainBudget | None = None) -> list[Event]:
        budget = budget or self.default_drain_budget
        result = DrainResult()
        while True:
            if budget.exhausted_by(
                dispatches=result.dispatches,
                activations=result.activations,
                cycles=result.cycles,
            ):
                result.exhausted = True
                break
            result.cycles += 1
            allowance = budget.dispatch_allowance(result.dispatches)
            delivered = 0
            if allowance is None or allowance > 0:
                delivered = self.dispatcher.dispatch_pending(allowance)
                result.dispatches += delivered
            instance = self.scheduler.next_runnable()
            if instance is None:
                if delivered == 0:
                    break
                continue
            activation = await self.executor.execute(instance)
            result.activations += 1
            result.produced_events.extend(activation.emitted_events)
            for event in self.join_coordinator.on_instance_finished(instance):
                self.event_store.append(event)
                result.produced_events.append(event)
        result.remaining_deliveries = self.event_delivery_store.count_pending()
        result.remaining_runnable_processes = len(
            self.process_store.instances_by_status(ProcessStatus.RUNNABLE)
        )
        self.last_drain = result
        return result.produced_events

    def recover(self) -> list[ProcessInstance]:
        """Recover interrupted deliveries and RUNNING Process instances."""
        self.dispatcher.backfill_legacy()
        self.dispatcher.recover()
        recovered = []
        for instance in self.process_store.instances_by_status(ProcessStatus.RUNNING):
            if self.process_store.get_definition(
                instance.definition_name, instance.definition_version
            ) is None:
                continue
            instance.status = ProcessStatus.RUNNABLE
            instance.updated_at = self.clock.now()
            self.process_store.save_instance(instance)
            recovered.append(instance)
        return recovered

    def close(self) -> None:
        """Close the SQLite connection."""
        self.db.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
