"""Persistence for process definitions and instances."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.process import ProcessDefinition, ProcessInstance, ProcessStatus
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ProcessStore:
    """Reads and writes :class:`ProcessDefinition` and :class:`ProcessInstance`."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- definitions -------------------------------------------------------

    def upsert_definition(self, definition: ProcessDefinition) -> None:
        """Insert or replace a process definition (idempotent by name+version)."""
        self.db.execute(
            """
            INSERT INTO process_definitions (name, version, handler, trigger_event_types)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name, version) DO UPDATE SET
                handler = excluded.handler,
                trigger_event_types = excluded.trigger_event_types
            """,
            (
                definition.name,
                definition.version,
                definition.handler,
                dumps(list(definition.trigger_event_types)),
            ),
        )

    def get_definition(self, name: str, version: str) -> ProcessDefinition | None:
        """Return the definition for ``name``/``version``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM process_definitions WHERE name = ? AND version = ?",
            (name, version),
        )
        return self._row_to_definition(row) if row else None

    def definitions_for_trigger(self, event_type: str) -> list[ProcessDefinition]:
        """Return every definition triggered by ``event_type``."""
        rows = self.db.query("SELECT * FROM process_definitions")
        result = []
        for row in rows:
            definition = self._row_to_definition(row)
            if event_type in definition.trigger_event_types:
                result.append(definition)
        return result

    # --- instances ---------------------------------------------------------

    def save_instance(self, instance: ProcessInstance) -> None:
        """Insert or update a process instance."""
        self.db.execute(
            """
            INSERT INTO process_instances
                (id, definition_name, definition_version, status, input, local_state,
                 parent_process_id, priority, pending_event_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                input = excluded.input,
                local_state = excluded.local_state,
                parent_process_id = excluded.parent_process_id,
                priority = excluded.priority,
                pending_event_id = excluded.pending_event_id,
                updated_at = excluded.updated_at
            """,
            (
                str(instance.id),
                instance.definition_name,
                instance.definition_version,
                instance.status.value,
                dumps(instance.input),
                dumps(instance.local_state),
                str(instance.parent_process_id) if instance.parent_process_id else None,
                instance.priority,
                str(instance.pending_event_id) if instance.pending_event_id else None,
                instance.created_at.isoformat(),
                instance.updated_at.isoformat(),
            ),
        )

    def get_instance(self, instance_id: uuid.UUID) -> ProcessInstance | None:
        """Return the instance with ``instance_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM process_instances WHERE id = ?", (str(instance_id),)
        )
        return self._row_to_instance(row) if row else None

    def all_instances(self) -> list[ProcessInstance]:
        """Return all instances ordered by creation time."""
        rows = self.db.query("SELECT * FROM process_instances ORDER BY created_at ASC")
        return [self._row_to_instance(r) for r in rows]

    def instances_by_status(self, status: ProcessStatus) -> list[ProcessInstance]:
        """Return all instances currently in ``status``."""
        rows = self.db.query(
            "SELECT * FROM process_instances WHERE status = ? ORDER BY created_at ASC",
            (status.value,),
        )
        return [self._row_to_instance(r) for r in rows]

    def next_runnable(self) -> ProcessInstance | None:
        """Return the highest-priority RUNNABLE instance (oldest breaks ties)."""
        row = self.db.query_one(
            """
            SELECT * FROM process_instances
            WHERE status = ?
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (ProcessStatus.RUNNABLE.value,),
        )
        return self._row_to_instance(row) if row else None

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _row_to_definition(row) -> ProcessDefinition:
        return ProcessDefinition(
            name=row["name"],
            version=row["version"],
            handler=row["handler"],
            trigger_event_types=tuple(loads(row["trigger_event_types"]) or ()),
        )

    @staticmethod
    def _row_to_instance(row) -> ProcessInstance:
        return ProcessInstance(
            definition_name=row["definition_name"],
            definition_version=row["definition_version"],
            id=uuid.UUID(row["id"]),
            status=ProcessStatus(row["status"]),
            input=loads(row["input"]),
            local_state=loads(row["local_state"]),
            parent_process_id=_uuid(row["parent_process_id"]),
            priority=row["priority"],
            pending_event_id=_uuid(row["pending_event_id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
