"""Persistence for process definitions and instances."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..context.requirements import ContextRequirements
from ..core.process import ProcessDefinition, ProcessInstance, ProcessStatus
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class ProcessStore:
    """Reads and writes :class:`ProcessDefinition` and :class:`ProcessInstance`."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- definitions -------------------------------------------------------

    def upsert_definition(self, definition: ProcessDefinition) -> None:
        """Insert or replace a process definition (idempotent by name+version)."""
        self.db.execute(
            """
            INSERT INTO process_definitions
                (name, version, handler, trigger_event_types, max_retries, metadata,
                 context_requirements)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, version) DO UPDATE SET
                handler = excluded.handler,
                trigger_event_types = excluded.trigger_event_types,
                max_retries = excluded.max_retries,
                metadata = excluded.metadata,
                context_requirements = excluded.context_requirements
            """,
            (
                definition.name,
                definition.version,
                definition.handler,
                dumps(list(definition.trigger_event_types)),
                definition.max_retries,
                dumps(definition.metadata),
                dumps(definition.context_requirements.to_dict())
                if definition.context_requirements is not None
                else None,
            ),
        )

    def get_definition(self, name: str, version: str) -> ProcessDefinition | None:
        """Return the definition for ``name``/``version``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM process_definitions WHERE name = ? AND version = ?",
            (name, version),
        )
        return self._row_to_definition(row) if row else None

    def all_definitions(self) -> list[ProcessDefinition]:
        """Return every registered definition, by name then version."""
        rows = self.db.query(
            "SELECT * FROM process_definitions ORDER BY name ASC, version ASC"
        )
        return [self._row_to_definition(r) for r in rows]

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
                 parent_process_id, priority, pending_event_id, work_key,
                 work_requirement_id, trigger_event_id, plan_id, plan_node_id,
                 retry_count, max_retries,
                 next_retry_at, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                input = excluded.input,
                local_state = excluded.local_state,
                parent_process_id = excluded.parent_process_id,
                priority = excluded.priority,
                pending_event_id = excluded.pending_event_id,
                work_key = excluded.work_key,
                work_requirement_id = excluded.work_requirement_id,
                retry_count = excluded.retry_count,
                max_retries = excluded.max_retries,
                next_retry_at = excluded.next_retry_at,
                last_error = excluded.last_error,
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
                instance.work_key,
                str(instance.work_requirement_id) if instance.work_requirement_id else None,
                str(instance.trigger_event_id) if instance.trigger_event_id else None,
                str(instance.plan_id) if instance.plan_id else None,
                str(instance.plan_node_id) if instance.plan_node_id else None,
                instance.retry_count,
                instance.max_retries,
                instance.next_retry_at.isoformat() if instance.next_retry_at else None,
                instance.last_error,
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

    def find_by_work_key(self, work_key: str) -> list[ProcessInstance]:
        """Return all instances fulfilling ``work_key`` (any status)."""
        rows = self.db.query(
            "SELECT * FROM process_instances WHERE work_key = ? ORDER BY created_at ASC",
            (work_key,),
        )
        return [self._row_to_instance(r) for r in rows]

    def find_by_work_requirement_id(
        self, work_requirement_id: uuid.UUID
    ) -> list[ProcessInstance]:
        """Return all instances fulfilling ``work_requirement_id``."""
        rows = self.db.query(
            "SELECT * FROM process_instances WHERE work_requirement_id = ? "
            "ORDER BY created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._row_to_instance(r) for r in rows]

    def pause_for_work(self, work_requirement_id: uuid.UUID) -> None:
        """Park future activations for one Work without touching external effects."""
        self.db.execute(
            """UPDATE process_instances SET status=?, updated_at=?
               WHERE work_requirement_id=? AND status IN (?, ?)""",
            (ProcessStatus.PAUSED.value, datetime.now().astimezone().isoformat(),
             str(work_requirement_id), ProcessStatus.RUNNABLE.value,
             ProcessStatus.RETRY_WAIT.value),
        )

    def resume_for_work(self, work_requirement_id: uuid.UUID) -> None:
        """Make parked activations runnable; executor recompiles fresh Context."""
        self.db.execute(
            "UPDATE process_instances SET status=?, updated_at=? WHERE work_requirement_id=? AND status=?",
            (ProcessStatus.RUNNABLE.value, datetime.now().astimezone().isoformat(),
             str(work_requirement_id), ProcessStatus.PAUSED.value),
        )

    def update_priority_for_work(self, work_requirement_id: uuid.UUID, priority: int) -> None:
        self.db.execute(
            "UPDATE process_instances SET priority=?, updated_at=? WHERE work_requirement_id=?",
            (priority, datetime.now().astimezone().isoformat(), str(work_requirement_id)),
        )

    def find_by_trigger(
        self, event_id: uuid.UUID, name: str, version: str
    ) -> list[ProcessInstance]:
        """Return instances of ``name``/``version`` already started by ``event_id``.

        The routing idempotency key (Phase 3F): a re-dispatched event must not
        start a process it already started.
        """
        rows = self.db.query(
            "SELECT * FROM process_instances WHERE trigger_event_id = ? "
            "AND definition_name = ? AND definition_version = ?",
            (str(event_id), name, version),
        )
        return [self._row_to_instance(r) for r in rows]

    def children_of(self, parent_id: uuid.UUID) -> list[ProcessInstance]:
        """Return the child instances of ``parent_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM process_instances WHERE parent_process_id = ? "
            "ORDER BY created_at ASC",
            (str(parent_id),),
        )
        return [self._row_to_instance(r) for r in rows]

    def due_retries(self, now_iso: str) -> list[ProcessInstance]:
        """Return RETRY_WAIT instances whose ``next_retry_at`` has passed."""
        rows = self.db.query(
            """
            SELECT * FROM process_instances
            WHERE status = ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?
            ORDER BY next_retry_at ASC
            """,
            (ProcessStatus.RETRY_WAIT.value, now_iso),
        )
        return [self._row_to_instance(r) for r in rows]

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _row_to_definition(row) -> ProcessDefinition:
        return ProcessDefinition(
            name=row["name"],
            version=row["version"],
            handler=row["handler"],
            trigger_event_types=tuple(loads(row["trigger_event_types"]) or ()),
            max_retries=row["max_retries"],
            metadata=loads(row["metadata"]) or {},
            context_requirements=ContextRequirements.from_dict(
                loads(row["context_requirements"])
            ),
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
            work_key=row["work_key"],
            work_requirement_id=_uuid(row["work_requirement_id"]),
            trigger_event_id=_uuid(row["trigger_event_id"]),
            plan_id=_uuid(row["plan_id"]),
            plan_node_id=_uuid(row["plan_node_id"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            next_retry_at=_dt(row["next_retry_at"]),
            last_error=row["last_error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
