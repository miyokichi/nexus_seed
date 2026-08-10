"""Runtime — the NEXUS SEED Phase 1 event-driven engine.

The Runtime is *mechanism only*.  It holds no domain knowledge and makes no
"AI" decisions; all meaning lives in process handlers.  Its responsibilities:

    store events, route events, create processes, run processes, suspend
    processes, persist/match continuations, resume processes, persist state.

All durable state lives in SQLite, so a Runtime can be discarded and rebuilt
from the same database with no loss of process state — the core acceptance
property of Phase 1.

Basic loop::

    Event -> Process -> State -> Continuation -> Event -> Resume
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..core.event import Event, utcnow
from ..core.process import (
    Handler,
    HandlerRegistry,
    ProcessDefinition,
    ProcessInstance,
    ProcessStatus,
    SpawnSpec,
)
from ..storage.continuation_store import ContinuationStore
from ..storage.database import Database
from ..storage.event_store import EventStore
from ..storage.process_store import ProcessStore
from ..storage.state_store import StateStore
from .continuation_resolver import ContinuationResolver
from .executor import Executor
from .router import Router
from .scheduler import Scheduler

logger = logging.getLogger("nexus_seed.runtime")


class Runtime:
    """The Phase 1 runtime wiring together stores, router, scheduler, executor.

    Handlers are code and must be (re)registered on every construction via
    :meth:`register_process`; definitions and all other state are loaded from
    the SQLite database at ``db_path``.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        registry: HandlerRegistry | None = None,
    ) -> None:
        self.db = Database(db_path)
        self.event_store = EventStore(self.db)
        self.process_store = ProcessStore(self.db)
        self.continuation_store = ContinuationStore(self.db)
        self.state_store = StateStore(self.db)

        self.registry = registry or HandlerRegistry()
        self.resolver = ContinuationResolver(self.continuation_store, self.process_store)
        self.router = Router(self.process_store, self.resolver)
        self.scheduler = Scheduler(self.process_store)
        self.executor = Executor(
            self.registry,
            self.process_store,
            self.continuation_store,
            self.state_store,
            self.event_store,
        )

    # --- registration ------------------------------------------------------

    def register_process(
        self, definition: ProcessDefinition, handler: Handler
    ) -> None:
        """Persist ``definition`` and bind its ``handler`` callable in memory."""
        self.process_store.upsert_definition(definition)
        self.registry.register(definition.handler, handler)
        logger.info("registered process %s v%s", definition.name, definition.version)

    # --- event intake ------------------------------------------------------

    async def submit_event(self, event: Event) -> list[Event]:
        """Ingest an external event and run the system until it goes idle.

        Returns every event produced by processes while draining (useful for
        tests and demos).  The submitted event itself is not included.
        """
        self._ingest(event)
        return await self._drain()

    def _ingest(self, event: Event) -> None:
        """Persist an event and route it (may make instances RUNNABLE)."""
        self.event_store.append(event)
        self.router.route(event)

    async def _drain(self) -> list[Event]:
        """Run RUNNABLE processes until none remain, ingesting emitted events."""
        produced: list[Event] = []
        while True:
            instance = self.scheduler.next_runnable()
            if instance is None:
                break
            result = await self.executor.execute(instance)
            for emitted in result.emitted_events:
                produced.append(emitted)
                self._ingest(emitted)
            for spec in result.spawned_processes:
                self._spawn(spec, parent=instance)
        return produced

    def _spawn(self, spec: SpawnSpec, *, parent: ProcessInstance) -> ProcessInstance:
        """Create a RUNNABLE child instance from a :class:`SpawnSpec`."""
        instance = ProcessInstance(
            definition_name=spec.definition_name,
            definition_version=spec.definition_version,
            status=ProcessStatus.RUNNABLE,
            input=dict(spec.input),
            parent_process_id=parent.id,
            priority=spec.priority,
        )
        self.process_store.save_instance(instance)
        logger.info("spawned instance %s (child of %s)", instance.id, parent.id)
        return instance

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
