"""Scheduler — picks the next RUNNABLE process to execute.

Phase 1 scheduling is deliberately minimal: choose the highest-``priority``
RUNNABLE instance, breaking ties by creation order.  No time-slicing, no
fairness policy — just enough to select work.
"""

from __future__ import annotations

from ..core.process import ProcessInstance
from ..storage.process_store import ProcessStore


class Scheduler:
    """Selects RUNNABLE instances from the process store."""

    def __init__(self, process_store: ProcessStore) -> None:
        self.process_store = process_store

    def next_runnable(self) -> ProcessInstance | None:
        """Return the next instance to run, or ``None`` if none are RUNNABLE."""
        return self.process_store.next_runnable()
