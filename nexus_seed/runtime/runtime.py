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
from ..core.state import StateEntry, StateHistoryEntry
from ..world.provenance import Provenance, get_state_provenance
from .clock import Clock
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

        self.registry = registry or HandlerRegistry()
        self.resolver = ContinuationResolver(self.continuation_store, self.process_store)
        self.router = Router(self.process_store, self.resolver)
        self.scheduler = Scheduler(self.process_store)
        self.join_coordinator = JoinCoordinator(self.join_store, self.process_store)
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
            self.clock,
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
