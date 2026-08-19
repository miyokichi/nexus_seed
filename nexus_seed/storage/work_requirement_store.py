"""Persistence for :class:`~nexus_seed.work.work_requirement.WorkRequirement`."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..capabilities.models import CapabilityRequirement
from ..core.event import utcnow
from ..work.work_requirement import WorkRequirement, WorkStatus
from .database import Database, dumps, loads


def _text(value) -> str | None:
    """A stored identifier that is text, not a UUID."""
    text = str(value).strip() if value is not None else ""
    return text or None


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
                 source_state_delta_id, priority, status, metadata,
                 required_capabilities_json, missing_capabilities_json,
                 selected_definition_name, selected_definition_version,
                 available_input_types_json, required_output_types_json,
                 selected_plan_id, decision_preference_json, max_replans,
                 replan_count, objective, scope_json, project, human_priority,
                 deadline, input_resources_json, constraints_json,
                 completion_criteria_json, provider_directive_json, goal_id,
                 command_id, pre_pause_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                dumps([r.to_dict() for r in requirement.required_capabilities]),
                dumps(list(requirement.missing_capabilities)),
                requirement.selected_definition_name,
                requirement.selected_definition_version,
                dumps(list(requirement.available_input_types)),
                dumps(list(requirement.required_output_types)),
                str(requirement.selected_plan_id) if requirement.selected_plan_id else None,
                dumps(requirement.decision_preference.to_dict())
                if requirement.decision_preference is not None
                else None,
                requirement.max_replans,
                requirement.replan_count,
                requirement.objective,
                dumps(requirement.scope),
                requirement.project,
                requirement.human_priority,
                requirement.deadline.isoformat() if requirement.deadline else None,
                dumps(requirement.input_resources),
                dumps(requirement.constraints),
                dumps(requirement.completion_criteria),
                dumps(requirement.provider_directive) if requirement.provider_directive else None,
                str(requirement.goal_id) if requirement.goal_id else None,
                str(requirement.command_id) if requirement.command_id else None,
                requirement.pre_pause_status,
                requirement.created_at.isoformat(),
                requirement.updated_at.isoformat(),
            ),
        )
        return cur.rowcount > 0

    def record_replan(self, requirement_id: uuid.UUID, attempt: int) -> None:
        """Record that this need has now been replanned ``attempt`` times.

        Stored on the need rather than counted from plans: a plan can fail for
        reasons that never reach a replanning attempt, so the two numbers are
        not the same and the limit is about attempts (Invariant 82).
        """
        self.db.execute(
            "UPDATE work_requirements SET replan_count = ?, updated_at = ? WHERE id = ?",
            (attempt, utcnow().isoformat(), str(requirement_id)),
        )

    def record_match(
        self,
        requirement_id: uuid.UUID,
        *,
        status: WorkStatus | str | None = None,
        missing_capabilities: list[str] | None = None,
        selected_definition: tuple[str, str] | None = None,
    ) -> None:
        """Store the outcome of capability matching on a requirement.

        Keeps the decision with the need, so spawning does not have to re-run
        the match and a later reader can see what was chosen and what was not
        available.

        ``status=None`` leaves the lifecycle alone: *what we chose* and *where
        the work stands* are different facts, and the same activation may well
        set the second on its own (ALREADY_RUNNING, say).  Writing a status
        here unconditionally would clobber it.
        """
        name, version = selected_definition or (None, None)
        assignments = [
            "missing_capabilities_json = ?",
            "selected_definition_name = ?",
            "selected_definition_version = ?",
            "updated_at = ?",
        ]
        params: list = [
            dumps(list(missing_capabilities or [])),
            name,
            version,
            utcnow().isoformat(),
        ]
        if status is not None:
            assignments.insert(0, "status = ?")
            params.insert(0, status.value if isinstance(status, WorkStatus) else status)
        params.append(str(requirement_id))
        self.db.execute(
            f"UPDATE work_requirements SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )

    def set_selected_plan(self, requirement_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        """Point a requirement at the plan currently pursuing it (spec §130)."""
        self.db.execute(
            "UPDATE work_requirements SET selected_plan_id = ?, updated_at = ? WHERE id = ?",
            (str(plan_id), utcnow().isoformat(), str(requirement_id)),
        )

    def update_status(self, requirement_id: uuid.UUID, status: WorkStatus | str) -> None:
        """Transition a requirement to a new status."""
        value = status.value if isinstance(status, WorkStatus) else status
        self.db.execute(
            "UPDATE work_requirements SET status = ?, updated_at = ? WHERE id = ?",
            (value, utcnow().isoformat(), str(requirement_id)),
        )

    def pause(self, requirement_id: uuid.UUID) -> bool:
        """Persist PAUSED while remembering the lifecycle state to resume."""
        current = self.get(requirement_id)
        if current is None or current.resolved or current.status is WorkStatus.PAUSED:
            return False
        self.db.execute(
            "UPDATE work_requirements SET pre_pause_status=status, status=?, updated_at=? WHERE id=?",
            (WorkStatus.PAUSED.value, utcnow().isoformat(), str(requirement_id)),
        )
        return True

    def resume(self, requirement_id: uuid.UUID) -> WorkStatus | None:
        """Restore the pre-pause lifecycle state, defaulting to EXPECTED."""
        current = self.get(requirement_id)
        if current is None or current.status is not WorkStatus.PAUSED:
            return None
        try:
            restored = WorkStatus(current.pre_pause_status or WorkStatus.EXPECTED.value)
        except ValueError:
            restored = WorkStatus.EXPECTED
        self.db.execute(
            "UPDATE work_requirements SET status=?, pre_pause_status=NULL, updated_at=? WHERE id=?",
            (restored.value, utcnow().isoformat(), str(requirement_id)),
        )
        return restored

    def update_priority(self, requirement_id: uuid.UUID, name: str, value: int) -> None:
        self.db.execute(
            "UPDATE work_requirements SET human_priority=?, priority=?, updated_at=? WHERE id=?",
            (name, value, utcnow().isoformat(), str(requirement_id)),
        )

    def update_deadline(self, requirement_id: uuid.UUID, deadline: datetime) -> None:
        self.db.execute(
            "UPDATE work_requirements SET deadline=?, updated_at=? WHERE id=?",
            (deadline.isoformat(), utcnow().isoformat(), str(requirement_id)),
        )

    def update_provider_directive(self, requirement_id: uuid.UUID, directive: dict) -> None:
        self.db.execute(
            "UPDATE work_requirements SET provider_directive_json=?, updated_at=? WHERE id=?",
            (dumps(directive), utcnow().isoformat(), str(requirement_id)),
        )

    def for_goal(self, goal_id) -> list[WorkRequirement]:
        rows = self.db.query(
            "SELECT * FROM work_requirements WHERE goal_id=? ORDER BY created_at", (str(goal_id),)
        )
        return [self._row(row) for row in rows]

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
            required_capabilities=[
                CapabilityRequirement.from_dict(d)
                for d in (loads(row["required_capabilities_json"]) or [])
            ],
            missing_capabilities=loads(row["missing_capabilities_json"]) or [],
            selected_definition_name=row["selected_definition_name"],
            selected_definition_version=row["selected_definition_version"],
            available_input_types=loads(row["available_input_types_json"]) or [],
            required_output_types=loads(row["required_output_types_json"]) or [],
            selected_plan_id=_uuid(row["selected_plan_id"]),
            decision_preference=_preference(_column(row, "decision_preference_json")),
            max_replans=_column(row, "max_replans"),
            replan_count=_column(row, "replan_count") or 0,
            objective=_column(row, "objective"),
            scope=loads(_column(row, "scope_json")) or {},
            project=_column(row, "project"),
            human_priority=_column(row, "human_priority"),
            deadline=(datetime.fromisoformat(_column(row, "deadline"))
                      if _column(row, "deadline") else None),
            input_resources=loads(_column(row, "input_resources_json")) or [],
            constraints=loads(_column(row, "constraints_json")) or {},
            completion_criteria=loads(_column(row, "completion_criteria_json")) or [],
            provider_directive=loads(_column(row, "provider_directive_json")),
            goal_id=_text(_column(row, "goal_id")),
            command_id=_uuid(_column(row, "command_id")),
            pre_pause_status=_column(row, "pre_pause_status"),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


def _column(row, name):
    """Read a column that a database from an earlier phase may not have."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _preference(raw):
    from ..decision.models import DecisionPreference

    data = loads(raw) if raw else None
    return DecisionPreference.from_dict(data) if data else None
