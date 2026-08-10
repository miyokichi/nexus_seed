"""Persistence for process activations — the idempotency ledger.

Each time a process activation commits its effects, its ``activation_key``
(``"{instance_id}:{event_id}"``) is recorded here.  The executor checks the key
before applying so a re-delivered event or a replayed activation cannot apply
the same side effects twice.
"""

from __future__ import annotations

import uuid

from ..core.event import utcnow
from .database import Database


def activation_key(instance_id: uuid.UUID, event_id: uuid.UUID | None) -> str:
    """Build the idempotency key for one activation."""
    return f"{instance_id}:{event_id or 'none'}"


class ActivationStore:
    """Records committed activations to guarantee idempotent side effects."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def exists(self, key: str) -> bool:
        """Return whether an activation with ``key`` has already committed."""
        row = self.db.query_one(
            "SELECT 1 FROM process_activations WHERE activation_key = ?", (key,)
        )
        return row is not None

    def record(
        self, key: str, instance_id: uuid.UUID, event_id: uuid.UUID | None
    ) -> None:
        """Record an activation as committed (ignored if already present)."""
        self.db.execute(
            """
            INSERT OR IGNORE INTO process_activations
                (activation_key, instance_id, event_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                key,
                str(instance_id),
                str(event_id) if event_id else None,
                utcnow().isoformat(),
            ),
        )
