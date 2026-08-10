"""Persistence for :class:`~nexus_seed.work.work_requirement.WorkRequirement`."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.event import utcnow
from ..work.work_requirement import WorkRequirement, WorkStatus
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class WorkRequirementStore:
    """Stores work requirements, keyed by a unique ``work_key`` for idempotency."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, requirement: WorkRequirement) -> bool:
        """Insert a requirement; ignore if its ``work_key`` already exists.

        Returns ``True`` if a new row was inserted, ``False`` if a requirement
        with the same ``work_key`` already existed (idempotency).
        """
        cur = self.db.execute(
            """
            INSERT OR IGNORE INTO work_requirements
                (id, work_type, work_key, related_entities, reason, source_event_id,
                 source_state_delta_id, priority, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(requirement.id),
                requirement.work_type,
                requirement.work_key,
                dumps(requirement.related_entities),
                requirement.reason,
                str(requirement.source_event_id) if requirement.source_event_id else None,
                str(requirement.source_state_delta_id)
                if requirement.source_state_delta_id
                else None,
                requirement.priority,
                requirement.status.value,
                dumps(requirement.metadata),
                requirement.created_at.isoformat(),
                requirement.updated_at.isoformat(),
            ),
        )
        return cur.rowcount > 0

    def update_status(self, requirement_id: uuid.UUID, status: WorkStatus | str) -> None:
        """Transition a requirement to a new status."""
        value = status.value if isinstance(status, WorkStatus) else status
        self.db.execute(
            "UPDATE work_requirements SET status = ?, updated_at = ? WHERE id = ?",
            (value, utcnow().isoformat(), str(requirement_id)),
        )

    def get(self, requirement_id: uuid.UUID) -> WorkRequirement | None:
        """Return the requirement with ``requirement_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM work_requirements WHERE id = ?", (str(requirement_id),)
        )
        return self._row(row) if row else None

    def get_by_work_key(self, work_key: str) -> WorkRequirement | None:
        """Return the requirement with ``work_key``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM work_requirements WHERE work_key = ?", (work_key,)
        )
        return self._row(row) if row else None

    def all(self) -> list[WorkRequirement]:
        """Return all requirements in creation order."""
        rows = self.db.query("SELECT * FROM work_requirements ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    def by_status(self, status: WorkStatus | str) -> list[WorkRequirement]:
        """Return all requirements in ``status``."""
        value = status.value if isinstance(status, WorkStatus) else status
        rows = self.db.query(
            "SELECT * FROM work_requirements WHERE status = ? ORDER BY created_at ASC",
            (value,),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> WorkRequirement:
        return WorkRequirement(
            work_type=row["work_type"],
            work_key=row["work_key"],
            related_entities=loads(row["related_entities"]) or [],
            reason=row["reason"] or "",
            source_event_id=_uuid(row["source_event_id"]),
            source_state_delta_id=_uuid(row["source_state_delta_id"]),
            priority=row["priority"],
            status=WorkStatus(row["status"]),
            metadata=loads(row["metadata"]) or {},
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
