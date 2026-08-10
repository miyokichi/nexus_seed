"""Impact — the consequences of a world-state change.

Domain data produced by the ``impact_analysis`` process.  An Impact names the
entities affected by a change and the work it is expected to require.  Phase 2C
does not require persisting Impact (its expected work is persisted as
:class:`WorkRequirement` rows); it is kept as a value object for clarity/trace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .work_requirement import WorkRequirement


@dataclass
class Impact:
    """The analysed consequence of a single state change."""

    source_event_id: uuid.UUID | None
    source_state_delta_id: uuid.UUID | None
    affected_entities: list[str] = field(default_factory=list)
    expected_work: list[WorkRequirement] = field(default_factory=list)
    reason: str | None = None
