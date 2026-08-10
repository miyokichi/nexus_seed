"""Continuation — how a suspended Process is resumed.

A Continuation is the *logical* (persistable) description of where a process
paused and what it is waiting for.  It deliberately does **not** capture the
Python call stack: only data that can be written to SQLite and rebuilt later.

    process paused at ``resume_point`` and is ``waiting_for`` a matching Event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .event import utcnow


@dataclass
class Continuation:
    """A logical continuation of a suspended process instance.

    Attributes:
        process_instance_id: The instance this continuation belongs to.
        resume_point: A handler-defined label for where to resume.
        waiting_for: A dict of conditions matched against future events.
        saved_process_state: Data the handler needs when it resumes.
        context_ref: Optional pointer to an externally stored context.
        id: Unique identifier.
        created_at: When the continuation was created (UTC).
    """

    process_instance_id: uuid.UUID
    resume_point: str
    waiting_for: dict = field(default_factory=dict)
    saved_process_state: dict = field(default_factory=dict)
    context_ref: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
