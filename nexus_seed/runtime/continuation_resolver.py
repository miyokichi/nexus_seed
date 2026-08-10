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

    Two ``waiting_for`` shapes are supported:

    * a flat dict — every key must match, where ``event_type`` compares against
      the event's type and any other key compares against the event payload;
    * ``{"any": [cond, ...]}`` — matches if *any* flat condition matches
      (the basis for "event OR timer" waits).
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

    @classmethod
    def matches(cls, waiting_for: dict, event: Event) -> bool:
        """Return ``True`` if ``event`` satisfies ``waiting_for``."""
        if "any" in waiting_for:
            return any(cls._matches_flat(cond, event) for cond in waiting_for["any"])
        return cls._matches_flat(waiting_for, event)

    @staticmethod
    def _matches_flat(condition: dict, event: Event) -> bool:
        for key, expected in condition.items():
            if key == "event_type":
                if event.type != expected:
                    return False
            elif event.payload.get(key) != expected:
                return False
        return True
