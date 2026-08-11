"""ProcessContextView — the read-only view a process sees at run time.

The view is what the :class:`~nexus_seed.context.compiler.ContextCompiler`
produces from persistent Memory according to a process's
:class:`~nexus_seed.context.requirements.ContextRequirements`.  It is:

* **read-only** — a process cannot mutate world state through it (writes stay
  declarative via ProcessResult);
* **selective** — only the declared slices are present;
* **provenance-bearing** — every item keeps its persistent record id;
* **regenerable** — never the source of truth; recompiled every activation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.continuation import Continuation
from ..core.event import Event, utcnow
from ..core.process import ProcessInstance
from ..core.state import StateEntry
from ..world.observation import Observation
from ..world.state_delta import StateDelta
from ..work.work_requirement import WorkRequirement


@dataclass(frozen=True)
class ContextMetadata:
    """Lightweight size/shape metadata (no token counting in Phase 3A)."""

    item_counts: dict[str, int] = field(default_factory=dict)
    approx_size_bytes: int = 0


@dataclass(frozen=True)
class ProcessContextView:
    """A compiled, read-only view of the Memory a process needs this activation.

    Attributes:
        process_instance: The activating instance (always present).
        trigger_event: The triggering/resuming event, if any.
        world_state: ``{entity: {attribute: StateEntry}}`` (only declared ones).
        recent_events / observations / state_deltas: selected history items.
        work_requirements: work relevant to this process.
        parent_process / child_processes / child_results: process-tree slices.
        continuation: the active continuation, if requested.
        metadata: item counts + approximate serialized size.
        compiled_at: when this view was compiled (UTC).
    """

    process_instance: ProcessInstance
    trigger_event: Event | None = None
    world_state: dict[str, dict[str, StateEntry]] = field(default_factory=dict)
    recent_events: list[Event] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    state_deltas: list[StateDelta] = field(default_factory=list)
    work_requirements: list[WorkRequirement] = field(default_factory=list)
    parent_process: ProcessInstance | None = None
    child_processes: list[ProcessInstance] = field(default_factory=list)
    child_results: list[dict] = field(default_factory=list)
    continuation: Continuation | None = None
    metadata: ContextMetadata = field(default_factory=ContextMetadata)
    compiled_at: datetime = field(default_factory=utcnow)

    # --- read helpers ------------------------------------------------------

    def get_state(self, entity: str, attribute: str, default: Any = None) -> Any:
        """Return a world-state value present in this view, or ``default``."""
        entry = self.get_state_entry(entity, attribute)
        return entry.value if entry is not None else default

    def get_state_entry(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the :class:`StateEntry` for a fact present in this view."""
        return self.world_state.get(entity, {}).get(attribute)

    @property
    def current_work_requirement(self) -> WorkRequirement | None:
        """Return the WorkRequirement this process fulfils, if present."""
        target = self.process_instance.work_requirement_id
        if target is not None:
            for requirement in self.work_requirements:
                if requirement.id == target:
                    return requirement
        return self.work_requirements[0] if self.work_requirements else None

    # --- serialization (for ContextSnapshot audit) ------------------------

    def to_snapshot_dict(self) -> dict:
        """Serialize to a compact JSON-safe dict, preserving record ids."""
        return {
            "process_instance_id": str(self.process_instance.id),
            "trigger_event_id": str(self.trigger_event.id) if self.trigger_event else None,
            "world_state": {
                entity: {
                    attr: {
                        "value": entry.value,
                        "version": entry.version,
                        "history_id": str(entry.history_id) if entry.history_id else None,
                    }
                    for attr, entry in attrs.items()
                }
                for entity, attrs in self.world_state.items()
            },
            "recent_events": [
                {"id": str(e.id), "type": e.type, "occurred_at": e.occurred_at.isoformat()}
                for e in self.recent_events
            ],
            "observations": [
                {"id": str(o.id), "subject": o.subject, "predicate": o.predicate}
                for o in self.observations
            ],
            "state_deltas": [
                {"id": str(d.id), "entity": d.entity, "attribute": d.attribute,
                 "new_value": d.new_value}
                for d in self.state_deltas
            ],
            "work_requirements": [
                {"id": str(w.id), "work_key": w.work_key, "status": w.status.value}
                for w in self.work_requirements
            ],
            "parent_process_id": str(self.parent_process.id) if self.parent_process else None,
            "child_process_ids": [str(c.id) for c in self.child_processes],
            "child_results": self.child_results,
            "continuation_id": str(self.continuation.id) if self.continuation else None,
            "metadata": {
                "item_counts": self.metadata.item_counts,
                "approx_size_bytes": self.metadata.approx_size_bytes,
            },
            "compiled_at": self.compiled_at.isoformat(),
        }


@dataclass
class ContextSnapshot:
    """An audit record of what a process activation compiled (not the truth).

    Stored so "what did this activation see at the time" can be inspected later,
    even after the world changes.  It is never used as a resume context.
    """

    process_instance_id: uuid.UUID
    context_json: dict
    trigger_event_id: uuid.UUID | None = None
    activation_id: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    compiled_at: datetime = field(default_factory=utcnow)
