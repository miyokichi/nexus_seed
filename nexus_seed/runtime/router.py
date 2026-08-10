"""Event Router — the entry point that turns an event into work.

For each incoming event the router decides two independent things:

1. Does the event **resume** any suspended process (via the resolver)?
2. Does the event **trigger** a fresh instance of any definition?

Both may happen for the same event.  Phase 1 uses plain rule-based matching —
no LLM, no inference.  The router only marks instances RUNNABLE; actually
running them is the scheduler/executor's job.
"""

from __future__ import annotations

import logging

from ..core.event import Event, utcnow
from ..core.process import ProcessInstance, ProcessStatus
from ..storage.process_store import ProcessStore
from .continuation_resolver import ContinuationResolver

logger = logging.getLogger("nexus_seed.runtime.router")


class Router:
    """Routes events to new or resumed process instances."""

    def __init__(
        self, process_store: ProcessStore, resolver: ContinuationResolver
    ) -> None:
        self.process_store = process_store
        self.resolver = resolver

    def route(self, event: Event) -> list[ProcessInstance]:
        """Route ``event``, returning the instances it made RUNNABLE."""
        activated: list[ProcessInstance] = []
        activated.extend(self._resume_matching(event))
        activated.extend(self._trigger_new(event))
        return activated

    def _resume_matching(self, event: Event) -> list[ProcessInstance]:
        activated: list[ProcessInstance] = []
        for instance, continuation in self.resolver.resolve(event):
            instance.status = ProcessStatus.RUNNABLE
            instance.pending_event_id = event.id
            instance.updated_at = utcnow()
            self.process_store.save_instance(instance)
            logger.info(
                "event %s (%s) resumes instance %s at %s",
                event.id,
                event.type,
                instance.id,
                continuation.resume_point,
            )
            activated.append(instance)
        return activated

    def _trigger_new(self, event: Event) -> list[ProcessInstance]:
        activated: list[ProcessInstance] = []
        for definition in self.process_store.definitions_for_trigger(event.type):
            instance = ProcessInstance(
                definition_name=definition.name,
                definition_version=definition.version,
                status=ProcessStatus.RUNNABLE,
                input={"trigger_event_id": str(event.id), "payload": event.payload},
                pending_event_id=event.id,
            )
            self.process_store.save_instance(instance)
            logger.info(
                "event %s (%s) triggers new instance %s of %s",
                event.id,
                event.type,
                instance.id,
                definition.name,
            )
            activated.append(instance)
        return activated
