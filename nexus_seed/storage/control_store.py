"""SQLite persistence for Phase 5G identities, commands, results and goals."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import uuid

from ..control.models import (
    Command,
    CommandResult,
    CommandStatus,
    Goal,
    GoalStatus,
    HumanIdentity,
    WorkConstraints,
    WorkPriority,
)
from ..core.event import utcnow
from .database import Database, dumps, loads


class ControlStore:
    """Durable audit and Goal repository for the human control plane."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_identity(self, identity: HumanIdentity) -> HumanIdentity:
        self.db.execute(
            """INSERT INTO human_identities
               (identity_id, display_name, permissions_json, enabled, metadata_json,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(identity_id) DO UPDATE SET
                 display_name=excluded.display_name,
                 permissions_json=excluded.permissions_json,
                 enabled=excluded.enabled,
                 metadata_json=excluded.metadata_json,
                 updated_at=excluded.updated_at""",
            (identity.identity_id, identity.display_name, dumps(list(identity.permissions)),
             int(identity.enabled), dumps(identity.metadata), identity.created_at.isoformat(),
             identity.updated_at.isoformat()),
        )
        return identity

    def get_identity(self, identity_id: str) -> HumanIdentity | None:
        row = self.db.query_one("SELECT * FROM human_identities WHERE identity_id=?", (identity_id,))
        if row is None:
            return None
        return HumanIdentity(
            identity_id=row["identity_id"], display_name=row["display_name"],
            permissions=tuple(loads(row["permissions_json"]) or ()),
            enabled=bool(row["enabled"]), metadata=loads(row["metadata_json"]) or {},
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_command(self, command: Command) -> bool:
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO commands
               (id, command_type, issuer_identity_id, source_channel, source_message_id,
                target_type, target_id, arguments_json, status, idempotency_key,
                validation_reasons_json, created_at, executed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(command.id), command.command_type, command.issuer_identity_id,
             command.source_channel, command.source_message_id, command.target_type,
             command.target_id, dumps(command.arguments), command.status.value,
             command.idempotency_key, dumps(command.validation_reasons),
             command.created_at.isoformat(),
             command.executed_at.isoformat() if command.executed_at else None),
        )
        return cursor.rowcount > 0

    def update_command(self, command: Command) -> None:
        self.db.execute(
            """UPDATE commands SET status=?, target_type=?, target_id=?, arguments_json=?,
               validation_reasons_json=?, executed_at=? WHERE id=?""",
            (command.status.value, command.target_type, command.target_id,
             dumps(command.arguments), dumps(command.validation_reasons),
             command.executed_at.isoformat() if command.executed_at else None,
             str(command.id)),
        )

    def get_command(self, command_id) -> Command | None:
        row = self.db.query_one("SELECT * FROM commands WHERE id=?", (str(command_id),))
        return self._command(row) if row else None

    def command_by_idempotency_key(self, key: str) -> Command | None:
        row = self.db.query_one("SELECT * FROM commands WHERE idempotency_key=?", (key,))
        return self._command(row) if row else None

    def save_result(self, result: CommandResult) -> None:
        self.db.execute(
            """INSERT INTO command_results
               (command_id, status, affected_entities_json, emitted_events_json,
                message, failure_reason, data_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(command_id) DO UPDATE SET status=excluded.status,
                 affected_entities_json=excluded.affected_entities_json,
                 emitted_events_json=excluded.emitted_events_json,
                 message=excluded.message, failure_reason=excluded.failure_reason,
                 data_json=excluded.data_json, created_at=excluded.created_at""",
            (str(result.command_id), result.status.value, dumps(result.affected_entities),
             dumps(result.emitted_events), result.message, result.failure_reason,
             dumps(result.data), result.created_at.isoformat()),
        )

    def get_result(self, command_id) -> CommandResult | None:
        row = self.db.query_one("SELECT * FROM command_results WHERE command_id=?", (str(command_id),))
        if row is None:
            return None
        return CommandResult(
            command_id=uuid.UUID(row["command_id"]), status=CommandStatus(row["status"]),
            affected_entities=loads(row["affected_entities_json"]) or [],
            emitted_events=loads(row["emitted_events_json"]) or [], message=row["message"] or "",
            failure_reason=row["failure_reason"], data=loads(row["data_json"]) or {},
            created_at=datetime.fromisoformat(row["created_at"]),
        )

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
    def _command(row) -> Command:
        return Command(
            command_type=row["command_type"], issuer_identity_id=row["issuer_identity_id"],
            source_channel=row["source_channel"], source_message_id=row["source_message_id"],
            target_type=row["target_type"], target_id=row["target_id"],
            arguments=loads(row["arguments_json"]) or {}, status=CommandStatus(row["status"]),
            idempotency_key=row["idempotency_key"],
            validation_reasons=loads(row["validation_reasons_json"]) or [],
            id=uuid.UUID(row["id"]), created_at=datetime.fromisoformat(row["created_at"]),
            executed_at=datetime.fromisoformat(row["executed_at"]) if row["executed_at"] else None,
        )

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
