"""Persistence for joins — parent processes waiting on spawned children."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from ..core.event import utcnow
from .database import Database, dumps, loads


@dataclass
class JoinRecord:
    """Tracks a parent's wait for its child processes.

    Attributes:
        parent_instance_id: The suspended parent.
        child_ids: The children being awaited.
        mode: ``"all"`` or ``"any"``.
        resume_point: Where the parent resumes when satisfied.
        saved_process_state: State returned to the parent on resume.
        completed: Children that have finished so far.
        satisfied: Whether the join condition has been met and signalled.
    """

    parent_instance_id: uuid.UUID
    child_ids: list[uuid.UUID]
    mode: str
    resume_point: str
    saved_process_state: dict = field(default_factory=dict)
    completed: list[uuid.UUID] = field(default_factory=list)
    satisfied: bool = False
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)

    def is_satisfied(self) -> bool:
        """Return whether ``completed`` meets ``mode`` over ``child_ids``."""
        if self.mode == "any":
            return len(self.completed) >= 1
        return set(self.completed) >= set(self.child_ids)


class JoinStore:
    """Stores :class:`JoinRecord` rows and finds joins by child."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, join: JoinRecord) -> JoinRecord:
        """Insert or update a join record."""
        self.db.execute(
            """
            INSERT INTO joins
                (id, parent_instance_id, child_ids, mode, resume_point,
                 saved_process_state, completed, satisfied, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                completed = excluded.completed,
                satisfied = excluded.satisfied
            """,
            (
                str(join.id),
                str(join.parent_instance_id),
                dumps([str(c) for c in join.child_ids]),
                join.mode,
                join.resume_point,
                dumps(join.saved_process_state),
                dumps([str(c) for c in join.completed]),
                int(join.satisfied),
                join.created_at.isoformat(),
            ),
        )
        return join

    def unsatisfied_for_child(self, child_id: uuid.UUID) -> list[JoinRecord]:
        """Return unsatisfied joins that await ``child_id``."""
        rows = self.db.query("SELECT * FROM joins WHERE satisfied = 0")
        result = []
        for row in rows:
            join = self._row(row)
            if child_id in join.child_ids:
                result.append(join)
        return result

    def get(self, join_id: uuid.UUID) -> JoinRecord | None:
        """Return the join with ``join_id``, or ``None``."""
        row = self.db.query_one("SELECT * FROM joins WHERE id = ?", (str(join_id),))
        return self._row(row) if row else None

    def all(self) -> list[JoinRecord]:
        """Return every join in creation order."""
        rows = self.db.query("SELECT * FROM joins ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> JoinRecord:
        return JoinRecord(
            parent_instance_id=uuid.UUID(row["parent_instance_id"]),
            child_ids=[uuid.UUID(c) for c in loads(row["child_ids"])],
            mode=row["mode"],
            resume_point=row["resume_point"],
            saved_process_state=loads(row["saved_process_state"]) or {},
            completed=[uuid.UUID(c) for c in loads(row["completed"])],
            satisfied=bool(row["satisfied"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
