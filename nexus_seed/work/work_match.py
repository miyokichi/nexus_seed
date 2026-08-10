"""WorkMatch — the result of checking a requirement against existing work."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum


class WorkMatchStatus(str, Enum):
    """Whether a requirement is already covered by an existing process."""

    NEW = "NEW"  # nothing exists for this work_key -> spawn needed
    ALREADY_RUNNING = "ALREADY_RUNNING"  # an active process already covers it
    ALREADY_COMPLETED = "ALREADY_COMPLETED"  # a completed process already did it
    DUPLICATE = "DUPLICATE"  # another identical requirement already handled it


@dataclass
class WorkMatch:
    """The outcome of matching a :class:`WorkRequirement` to existing work."""

    requirement_id: uuid.UUID
    status: WorkMatchStatus
    process_instance_id: uuid.UUID | None = None
    reason: str | None = None
