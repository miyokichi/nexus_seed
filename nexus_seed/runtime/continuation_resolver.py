"""Continuation Resolver — matches incoming events to waiting continuations."""

from __future__ import annotations

import logging

from ..core.continuation import Continuation
from ..core.event import Event
from ..core.process import ProcessInstance, ProcessStatus
from ..storage.continuation_store import ContinuationStore
from ..storage.process_store import ProcessStore

logger = logging.getLogger("nexus_seed.runtime.continuation_resolver")


class ContinuationResolver:
    """Finds suspended processes whose ``waiting_for`` an event satisfies.

    Matching is a simple dict comparison (Phase 1): every key in ``waiting_for``
    must match, where the special key ``event_type`` compares against the
    event's ``type`` and any other key compares against the event's payload.
    """

    def __init__(
        self, continuation_store: ContinuationStore, process_store: ProcessStore
    ) -> None:
        self.continuation_store = continuation_store
        self.process_store = process_store

    def resolve(self, event: Event) -> list[tuple[ProcessInstance, Continuation]]:
        """Return ``(instance, continuation)`` pairs this event unblocks."""
        matches: list[tuple[ProcessInstance, Continuation]] = []
        for continuation in self.continuation_store.all():
            if not self.matches(continuation.waiting_for, event):
                continue
            instance = self.process_store.get_instance(continuation.process_instance_id)
            if instance is None:
                logger.warning(
                    "continuation %s references missing instance %s",
                    continuation.id,
                    continuation.process_instance_id,
                )
                continue
            if instance.status is not ProcessStatus.SUSPENDED:
                continue
            matches.append((instance, continuation))
        return matches

    @staticmethod
    def matches(waiting_for: dict, event: Event) -> bool:
        """Return ``True`` if ``event`` satisfies every ``waiting_for`` condition."""
        for key, expected in waiting_for.items():
            if key == "event_type":
                if event.type != expected:
                    return False
            elif event.payload.get(key) != expected:
                return False
        return True
