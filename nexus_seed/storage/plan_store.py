"""Persistence for composed process plans.

A plan, its nodes and its edges are written together (spec §11): a half-written
plan is not a weaker plan, it is a wrong one — a graph missing an edge would
run in the wrong order.

The UNIQUE index on ``(plan_id, node_key)`` is what makes node execution
idempotent across restarts: a node is a position, and a position exists once.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.event import utcnow
from ..planning.models import (
    PlanEdge,
    PlanNode,
    PlanNodeStatus,
    PlanStatus,
    ProcessPlan,
)
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _column(row, name):
    """Read a column a database from an earlier phase may not have."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class PlanStore:
    """Stores composed plans, their nodes and their data dependencies."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- creation ----------------------------------------------------------

    def create(
        self, plan: ProcessPlan, nodes: list[PlanNode], edges: list[PlanEdge]
    ) -> ProcessPlan:
        """Persist a whole plan atomically."""
        with self.db.atomic():
            self._insert_plan(plan)
            for node in nodes:
                self.save_node(node)
            for edge in edges:
                self.save_edge(edge)
        return plan

    def _insert_plan(self, plan: ProcessPlan) -> None:
        self.db.execute(
            """
            INSERT INTO process_plans
                (id, work_requirement_id, status, planner_name, planner_version,
                 required_capabilities_json, input_types_json,
                 required_output_types_json, planning_snapshot_json, reasons_json,
                 created_by_process_id, fingerprint, replan_attempt,
                 supersedes_plan_id, created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                planning_snapshot_json = excluded.planning_snapshot_json,
                reasons_json = excluded.reasons_json,
                updated_at = excluded.updated_at
            """,
            (
                str(plan.id),
                str(plan.work_requirement_id),
                plan.status.value,
                plan.planner_name,
                plan.planner_version,
                dumps([r.to_dict() for r in plan.required_capabilities]),
                dumps(list(plan.input_types)),
                dumps(list(plan.required_output_types)),
                dumps(plan.planning_snapshot),
                dumps(list(plan.reasons)),
                str(plan.created_by_process_id) if plan.created_by_process_id else None,
                plan.fingerprint,
                plan.replan_attempt,
                str(plan.supersedes_plan_id) if plan.supersedes_plan_id else None,
                plan.created_at.isoformat(),
                utcnow().isoformat(),
                plan.completed_at.isoformat() if plan.completed_at else None,
            ),
        )

    def save_node(self, node: PlanNode) -> PlanNode:
        """Insert or update one plan node."""
        self.db.execute(
            """
            INSERT INTO plan_nodes
                (id, plan_id, node_key, definition_name, definition_version,
                 provided_capabilities_json, input_types_json, output_types_json,
                 status, process_instance_id, depth, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id, node_key) DO UPDATE SET
                status = excluded.status,
                process_instance_id = excluded.process_instance_id,
                updated_at = excluded.updated_at
            """,
            (
                str(node.id),
                str(node.plan_id),
                node.node_key,
                node.definition_name,
                node.definition_version,
                dumps(list(node.provided_capabilities)),
                dumps(list(node.input_types)),
                dumps(list(node.output_types)),
                node.status.value,
                str(node.process_instance_id) if node.process_instance_id else None,
                node.depth,
                node.created_at.isoformat(),
                utcnow().isoformat(),
            ),
        )
        return node

    def save_edge(self, edge: PlanEdge) -> PlanEdge:
        """Insert one binding.  Re-saving the same edge is a no-op.

        Deliberately *not* ``INSERT OR IGNORE``: that swallowed every conflict,
        including a second producer claiming an input another edge already
        feeds.  Dropping such an edge silently is how a consumer ends up
        running with a missing input while the plan still reports success, so
        the constraint is allowed to raise (Invariant 67).
        """
        self.db.execute(
            """
            INSERT INTO plan_edges
                (id, plan_id, from_node_id, to_node_id, artifact_type,
                 output_type, input_type, output_key, input_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                str(edge.id),
                str(edge.plan_id),
                str(edge.from_node_id),
                str(edge.to_node_id),
                edge.artifact_type or edge.output_type,
                edge.output_type,
                edge.input_type,
                edge.output_key,
                edge.input_key,
                edge.created_at.isoformat(),
            ),
        )
        return edge

    # --- transitions -------------------------------------------------------

    def update_status(self, plan_id: uuid.UUID, status: PlanStatus | str) -> None:
        """Transition a plan, stamping ``completed_at`` on a terminal status."""
        value = status.value if isinstance(status, PlanStatus) else status
        now = utcnow().isoformat()
        completed = now if PlanStatus(value).terminal else None
        self.db.execute(
            "UPDATE process_plans SET status = ?, updated_at = ?, "
            "completed_at = COALESCE(?, completed_at) WHERE id = ?",
            (value, now, completed, str(plan_id)),
        )

    def update_node(
        self,
        node_id: uuid.UUID,
        status: PlanNodeStatus | str,
        *,
        process_instance_id: uuid.UUID | None = None,
    ) -> None:
        """Transition a node, optionally binding the instance running it."""
        value = status.value if isinstance(status, PlanNodeStatus) else status
        if process_instance_id is not None:
            self.db.execute(
                "UPDATE plan_nodes SET status = ?, process_instance_id = ?, "
                "updated_at = ? WHERE id = ?",
                (value, str(process_instance_id), utcnow().isoformat(), str(node_id)),
            )
            return
        self.db.execute(
            "UPDATE plan_nodes SET status = ?, updated_at = ? WHERE id = ?",
            (value, utcnow().isoformat(), str(node_id)),
        )

    # --- reads -------------------------------------------------------------

    def get(self, plan_id: uuid.UUID) -> ProcessPlan | None:
        """Return a plan by id."""
        row = self.db.query_one(
            "SELECT * FROM process_plans WHERE id = ?", (str(plan_id),)
        )
        return self._plan(row) if row else None

    def for_work(self, work_requirement_id: uuid.UUID) -> list[ProcessPlan]:
        """Return every plan attempted for a requirement, oldest first."""
        rows = self.db.query(
            "SELECT * FROM process_plans WHERE work_requirement_id = ? "
            "ORDER BY created_at ASC",
            (str(work_requirement_id),),
        )
        return [self._plan(r) for r in rows]

    def active_for_work(self, work_requirement_id: uuid.UUID) -> ProcessPlan | None:
        """Return the plan currently being pursued for a requirement.

        At most one (spec §129): several plans may exist in history, but two
        running at once would race each other's nodes.
        """
        row = self.db.query_one(
            "SELECT * FROM process_plans WHERE work_requirement_id = ? "
            "AND status IN (?, ?) ORDER BY created_at DESC LIMIT 1",
            (
                str(work_requirement_id),
                PlanStatus.VALIDATED.value,
                PlanStatus.RUNNING.value,
            ),
        )
        return self._plan(row) if row else None

    def all(self) -> list[ProcessPlan]:
        """Return every plan, oldest first."""
        rows = self.db.query("SELECT * FROM process_plans ORDER BY created_at ASC")
        return [self._plan(r) for r in rows]

    def candidates_for_work(self, work_requirement_id: uuid.UUID) -> list[ProcessPlan]:
        """Plans for a need that could still be chosen (Phase 4C).

        ``PROPOSED`` is a composed, validated candidate awaiting selection;
        ``VALIDATED`` is the one already chosen.  Both are selectable, which
        makes re-running a selection a no-op rather than a second decision.
        """
        rows = self.db.query(
            "SELECT * FROM process_plans WHERE work_requirement_id = ? "
            "AND status IN (?, ?) ORDER BY created_at ASC",
            (
                str(work_requirement_id),
                PlanStatus.PROPOSED.value,
                PlanStatus.VALIDATED.value,
            ),
        )
        return [self._plan(r) for r in rows]

    def failed_fingerprints(self, work_requirement_id: uuid.UUID) -> list[str]:
        """The shapes that have already failed terminally for this need.

        What replanning excludes (spec §94): re-composing a plan that has
        already been tried and failed would produce the same failure, and doing
        that repeatedly is how a bounded retry becomes an unbounded loop.
        """
        rows = self.db.query(
            "SELECT DISTINCT fingerprint FROM process_plans "
            "WHERE work_requirement_id = ? AND status = ? AND fingerprint IS NOT NULL",
            (str(work_requirement_id), PlanStatus.FAILED.value),
        )
        return [r["fingerprint"] for r in rows]

    def by_status(self, status: PlanStatus | str) -> list[ProcessPlan]:
        """Return plans in a given status."""
        value = status.value if isinstance(status, PlanStatus) else status
        rows = self.db.query(
            "SELECT * FROM process_plans WHERE status = ? ORDER BY created_at ASC",
            (value,),
        )
        return [self._plan(r) for r in rows]

    def nodes(self, plan_id: uuid.UUID) -> list[PlanNode]:
        """Return a plan's nodes, shallowest first then by key."""
        rows = self.db.query(
            "SELECT * FROM plan_nodes WHERE plan_id = ? ORDER BY depth ASC, node_key ASC",
            (str(plan_id),),
        )
        return [self._node(r) for r in rows]

    def get_node(self, node_id: uuid.UUID) -> PlanNode | None:
        """Return one node by id."""
        row = self.db.query_one("SELECT * FROM plan_nodes WHERE id = ?", (str(node_id),))
        return self._node(row) if row else None

    def get_node_by_key(self, plan_id: uuid.UUID, node_key: str) -> PlanNode | None:
        """Return one node by its logical position in a plan."""
        row = self.db.query_one(
            "SELECT * FROM plan_nodes WHERE plan_id = ? AND node_key = ?",
            (str(plan_id), node_key),
        )
        return self._node(row) if row else None

    def edges(self, plan_id: uuid.UUID) -> list[PlanEdge]:
        """Return a plan's data dependencies."""
        rows = self.db.query(
            "SELECT * FROM plan_edges WHERE plan_id = ? ORDER BY created_at ASC",
            (str(plan_id),),
        )
        return [self._edge(r) for r in rows]

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _plan(row) -> ProcessPlan:
        from ..capabilities.models import CapabilityRequirement

        return ProcessPlan(
            work_requirement_id=uuid.UUID(row["work_requirement_id"]),
            status=PlanStatus(row["status"]),
            planner_name=row["planner_name"],
            planner_version=row["planner_version"],
            required_capabilities=[
                CapabilityRequirement.from_dict(d)
                for d in (loads(row["required_capabilities_json"]) or [])
            ],
            input_types=loads(row["input_types_json"]) or [],
            required_output_types=loads(row["required_output_types_json"]) or [],
            planning_snapshot=loads(row["planning_snapshot_json"]) or {},
            reasons=loads(row["reasons_json"]) or [],
            created_by_process_id=_uuid(row["created_by_process_id"]),
            fingerprint=_column(row, "fingerprint"),
            replan_attempt=_column(row, "replan_attempt") or 0,
            supersedes_plan_id=_uuid(_column(row, "supersedes_plan_id")),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=_dt(row["completed_at"]),
        )

    @staticmethod
    def _node(row) -> PlanNode:
        return PlanNode(
            plan_id=uuid.UUID(row["plan_id"]),
            node_key=row["node_key"],
            definition_name=row["definition_name"],
            definition_version=row["definition_version"],
            provided_capabilities=loads(row["provided_capabilities_json"]) or [],
            input_types=loads(row["input_types_json"]) or [],
            output_types=loads(row["output_types_json"]) or [],
            status=PlanNodeStatus(row["status"]),
            process_instance_id=_uuid(row["process_instance_id"]),
            depth=row["depth"],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _edge(row) -> PlanEdge:
        return PlanEdge(
            plan_id=uuid.UUID(row["plan_id"]),
            from_node_id=uuid.UUID(row["from_node_id"]),
            to_node_id=uuid.UUID(row["to_node_id"]),
            artifact_type=row["artifact_type"],
            output_type=row["output_type"],
            input_type=row["input_type"],
            output_key=row["output_key"],
            input_key=row["input_key"],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
