"""SQLite persistence for Goals."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import uuid

from ..control.models import Goal, GoalStatus, WorkConstraints, WorkPriority
from ..core.event import utcnow
from .database import Database, dumps, loads


class ControlStore:
    """Durable Goal repository.

    The audit half of this store — identities, commands and their results —
    went with the command surface.  Nothing writes those tables any more.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_goal(self, goal: Goal) -> bool:
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO goals
               (id, title, objective, owner_identity_id, scope_json, priority, deadline,
                constraints_json, success_criteria_json, status, metadata_json,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(goal.id), goal.title, goal.objective, goal.owner_identity_id,
             dumps(goal.scope), goal.priority.value,
             goal.deadline.isoformat() if goal.deadline else None,
             dumps(goal.constraints.to_dict()), dumps(goal.success_criteria), goal.status.value,
             dumps(goal.metadata), goal.created_at.isoformat(), goal.updated_at.isoformat()),
        )
        return cursor.rowcount > 0

    def update_goal_status(self, goal_id, status: GoalStatus | str) -> None:
        value = status.value if isinstance(status, GoalStatus) else str(status)
        self.db.execute("UPDATE goals SET status=?, updated_at=? WHERE id=?",
                        (value, utcnow().isoformat(), str(goal_id)))

    def get_goal(self, goal_id) -> Goal | None:
        row = self.db.query_one("SELECT * FROM goals WHERE id=?", (str(goal_id),))
        return self._goal(row) if row else None

    def goals(self, status: GoalStatus | str | None = None) -> list[Goal]:
        if status is None:
            rows = self.db.query("SELECT * FROM goals ORDER BY created_at")
        else:
            value = status.value if isinstance(status, GoalStatus) else str(status)
            rows = self.db.query("SELECT * FROM goals WHERE status=? ORDER BY created_at", (value,))
        return [self._goal(row) for row in rows]

    @staticmethod
    def _goal(row) -> Goal:
        return Goal(
            title=row["title"], objective=row["objective"],
            owner_identity_id=row["owner_identity_id"], scope=loads(row["scope_json"]) or {},
            priority=WorkPriority(row["priority"]),
            deadline=datetime.fromisoformat(row["deadline"]) if row["deadline"] else None,
            constraints=WorkConstraints.from_dict(loads(row["constraints_json"])),
            success_criteria=loads(row["success_criteria_json"]) or [],
            status=GoalStatus(row["status"]), metadata=loads(row["metadata_json"]) or {},
            id=uuid.UUID(row["id"]), created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
