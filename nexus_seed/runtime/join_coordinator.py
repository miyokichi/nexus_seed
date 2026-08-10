"""Join Coordinator — signals a parent when its awaited children finish.

Spawn/join is expressed with the ordinary Event + Continuation machinery: a
joining parent suspends on a ``join_satisfied`` event.  When a child reaches a
terminal status this coordinator records it against any join awaiting it and,
once the join's ``mode`` is met, emits the ``join_satisfied`` event (carrying
the children's results) which the resolver matches to resume the parent.
"""

from __future__ import annotations

import logging

from ..core.event import Event
from ..core.process import ProcessInstance, ProcessStatus
from ..storage.join_store import JoinStore
from ..storage.process_store import ProcessStore

logger = logging.getLogger("nexus_seed.runtime.join_coordinator")

_TERMINAL = (ProcessStatus.COMPLETED, ProcessStatus.FAILED)


class JoinCoordinator:
    """Tracks child completion and produces ``join_satisfied`` events."""

    def __init__(self, join_store: JoinStore, process_store: ProcessStore) -> None:
        self.join_store = join_store
        self.process_store = process_store

    def on_instance_finished(self, instance: ProcessInstance) -> list[Event]:
        """Record ``instance`` against its joins; return any satisfied signals."""
        if instance.status not in _TERMINAL:
            return []

        events: list[Event] = []
        for join in self.join_store.unsatisfied_for_child(instance.id):
            if instance.id not in join.completed:
                join.completed.append(instance.id)
            if not join.is_satisfied():
                self.join_store.save(join)
                continue

            join.satisfied = True
            self.join_store.save(join)
            events.append(
                Event(
                    type="join_satisfied",
                    source="runtime.join",
                    payload={
                        "join_id": str(join.id),
                        "parent_instance_id": str(join.parent_instance_id),
                        "children": self._child_results(join.completed),
                    },
                )
            )
            logger.info(
                "join %s satisfied (%s) for parent %s",
                join.id,
                join.mode,
                join.parent_instance_id,
            )
        return events

    def _child_results(self, child_ids) -> list[dict]:
        results = []
        for child_id in child_ids:
            child = self.process_store.get_instance(child_id)
            if child is None:
                continue
            results.append(
                {
                    "id": str(child_id),
                    "status": child.status.value,
                    "output": child.local_state.get("output"),
                }
            )
        return results
