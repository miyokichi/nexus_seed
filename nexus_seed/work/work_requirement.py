"""WorkRequirement — "this work needs doing" (a Need, not an execution).

Domain data, not a Runtime primitive.  A WorkRequirement expresses that some
work is *required*; the work is actually *executed* by a ProcessInstance:

    StateDelta      = the world changed
    Impact          = that change has these consequences
    WorkRequirement = this work needs doing
    ProcessInstance = this work is being done

A ``work_key`` gives each requirement a logical identity (including the world
state version), so the same change never spawns the same work twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..core.event import utcnow


class WorkStatus(str, Enum):
    """Lifecycle of a :class:`WorkRequirement` (distinct from ProcessStatus)."""

    EXPECTED = "EXPECTED"  # derived by impact analysis, not yet matched
    MATCHED = "MATCHED"  # already covered by existing work
    SPAWNED = "SPAWNED"  # a process was spawned to do it
    SATISFIED = "SATISFIED"  # the work completed
    CANCELLED = "CANCELLED"  # no longer needed
    #: Phase 4B: a composed multi-process plan was built for this need.
    PLANNED = "PLANNED"
    #: Phase 4A: the need stands, but nothing here can currently do it.
    #: Deliberately *not* CANCELLED (Invariant 51) — cancelling would throw
    #: away a real need because of a temporary limitation of our own, and
    #: acquiring the capability later could never revive it.
    BLOCKED_CAPABILITY = "BLOCKED_CAPABILITY"
    #: Phase 5E: the competence is represented by a ProcessDefinition, but no
    #: operational provider can execute it.  This is not a capability gap.
    BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
    #: Phase 4C: the competence exists, but no plan we can currently build and
    #: run satisfies the need — every candidate failed, or replanning hit its
    #: limit.  A narrower statement than BLOCKED_CAPABILITY and, for the same
    #: reason as it, still not CANCELLED (Invariant 79): the need is real; only
    #: our current arrangements for meeting it have run out.
    BLOCKED_PLAN = "BLOCKED_PLAN"


@dataclass
class WorkRequirement:
    """A required unit of work derived from a world-state change.

    Attributes:
        work_type: What kind of work (e.g. ``"resistance_check"``).
        related_entities: Entities the work concerns.
        reason: Why the work is required.
        work_key: Logical identity, e.g. ``"resistance_check:D1_CD:v2"``.
        source_event_id: The event that led to this requirement.
        source_state_delta_id: The delta that led to this requirement.
        priority: Scheduling priority for the spawned process.
        status: Current :class:`WorkStatus`.
        metadata: Free-form extras (room for ``depends_on`` etc. later).
        required_capabilities: What doing this work *takes* (Phase 4A).  When
            present this is what finds an implementation; ``work_type`` remains
            the logical name and the legacy lookup path.
        missing_capabilities: What was unavailable at the last matching attempt
            — the record of a gap in the system's own competence.
        selected_definition_name / selected_definition_version: The process
            capability matching chose, so spawning does not re-decide.
        id: Unique identifier.
        created_at / updated_at: Timestamps (UTC).
    """

    work_type: str
    work_key: str
    related_entities: list[str] = field(default_factory=list)
    reason: str = ""
    source_event_id: uuid.UUID | None = None
    source_state_delta_id: uuid.UUID | None = None
    priority: int = 0
    status: WorkStatus = WorkStatus.EXPECTED
    metadata: dict = field(default_factory=dict)
    required_capabilities: list = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    selected_definition_name: str | None = None
    selected_definition_version: str | None = None
    #: Phase 4B: what a composed plan may start from, and what it must produce.
    available_input_types: list[str] = field(default_factory=list)
    required_output_types: list[str] = field(default_factory=list)
    selected_plan_id: uuid.UUID | None = None
    #: Phase 4C: what this need is optimising for and what it will not accept.
    #: ``None`` means no preference was expressed, which is the ordinary case
    #: and must keep working exactly as before (spec §139).
    decision_preference: "DecisionPreference | None" = None
    #: How many times this need may be replanned after a terminal plan failure.
    #: ``None`` falls back to the policy default; replanning is always finite
    #: (Invariant 82).
    max_replans: int | None = None
    replan_count: int = 0
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def needs_capabilities(self) -> bool:
        """Whether this work declares capability requirements (vs. legacy)."""
        return bool(self.required_capabilities)

    @property
    def resolved(self) -> bool:
        """Whether this need has been met and must not be replanned (spec §91)."""
        return self.status in (WorkStatus.SATISFIED, WorkStatus.CANCELLED)
