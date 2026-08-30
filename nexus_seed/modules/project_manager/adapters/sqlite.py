"""Durable storage for the Project Orchestrator.

Projects, their Agents, and the A2A messages between them.  Same conventions as
every other store here: stdlib ``sqlite3``, JSON in TEXT columns, no ORM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ....core.event import utcnow
from ..models import (
    A2AMessage,
    A2AMessageType,
    Agent,
    AgentStatus,
    Project,
    ProjectStatus,
)
from ....storage.database import Database, dumps, loads


class ProjectStore:
    """Reads and writes :class:`Project` records."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, project: Project) -> Project:
        """Insert or update a project."""
        project.updated_at = utcnow()
        self.db.execute(
            """
            INSERT INTO orchestrator_projects
                (id, goal, context, status, priority, assigned_agent_id,
                 parent_project_id, summary, blockers, tasks, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                goal = excluded.goal,
                context = excluded.context,
                status = excluded.status,
                priority = excluded.priority,
                assigned_agent_id = excluded.assigned_agent_id,
                parent_project_id = excluded.parent_project_id,
                summary = excluded.summary,
                blockers = excluded.blockers,
                tasks = excluded.tasks,
                updated_at = excluded.updated_at
            """,
            (
                project.id,
                project.goal,
                dumps(project.context),
                project.status.value,
                project.priority,
                project.assigned_agent_id,
                project.parent_project_id,
                project.summary,
                dumps(project.blockers),
                dumps(project.tasks),
                project.created_at.isoformat(),
                project.updated_at.isoformat(),
            ),
        )
        return project

    def get(self, project_id: str) -> Project | None:
        """Return the project with ``project_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM orchestrator_projects WHERE id = ?", (project_id,)
        )
        return self._row(row) if row else None

    def all(self) -> list[Project]:
        """Return every project, oldest first."""
        rows = self.db.query("SELECT * FROM orchestrator_projects ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    def by_status(self, *statuses: ProjectStatus) -> list[Project]:
        """Return projects in any of ``statuses``, highest priority first."""
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self.db.query(
            f"SELECT * FROM orchestrator_projects WHERE status IN ({placeholders}) "
            "ORDER BY priority DESC, created_at ASC",
            tuple(s.value for s in statuses),
        )
        return [self._row(r) for r in rows]

    def children_of(self, parent_project_id: str) -> list[Project]:
        """Return the child projects of ``parent_project_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM orchestrator_projects WHERE parent_project_id = ? "
            "ORDER BY created_at ASC",
            (parent_project_id,),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Project:
        return Project(
            goal=row["goal"],
            context=loads(row["context"]) or {},
            status=ProjectStatus(row["status"]),
            priority=row["priority"],
            assigned_agent_id=row["assigned_agent_id"],
            parent_project_id=row["parent_project_id"],
            summary=row["summary"] or "",
            blockers=loads(row["blockers"]) or [],
            tasks=loads(row["tasks"]) or [],
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class AgentStore:
    """Reads and writes :class:`Agent` records."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, agent: Agent) -> Agent:
        """Insert or update an agent."""
        agent.updated_at = utcnow()
        self.db.execute(
            """
            INSERT INTO orchestrator_agents
                (agent_id, project_id, runtime, status, endpoint, metadata,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                project_id = excluded.project_id,
                runtime = excluded.runtime,
                status = excluded.status,
                endpoint = excluded.endpoint,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                agent.agent_id,
                agent.project_id,
                agent.runtime,
                agent.status.value,
                agent.endpoint,
                dumps(agent.metadata),
                agent.created_at.isoformat(),
                agent.updated_at.isoformat(),
            ),
        )
        return agent

    def get(self, agent_id: str) -> Agent | None:
        """Return the agent with ``agent_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM orchestrator_agents WHERE agent_id = ?", (agent_id,)
        )
        return self._row(row) if row else None

    def for_project(self, project_id: str) -> list[Agent]:
        """Return every agent ever assigned to ``project_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM orchestrator_agents WHERE project_id = ? ORDER BY created_at ASC",
            (project_id,),
        )
        return [self._row(r) for r in rows]

    def active_for_project(self, project_id: str) -> Agent | None:
        """Return the live agent owning ``project_id``, if there is one."""
        for agent in self.for_project(project_id):
            if agent.status in (AgentStatus.STARTING, AgentStatus.RUNNING, AgentStatus.IDLE):
                return agent
        return None

    def all(self) -> list[Agent]:
        """Return every agent, oldest first."""
        rows = self.db.query("SELECT * FROM orchestrator_agents ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Agent:
        return Agent(
            project_id=row["project_id"],
            runtime=row["runtime"],
            status=AgentStatus(row["status"]),
            endpoint=row["endpoint"],
            metadata=loads(row["metadata"]) or {},
            agent_id=row["agent_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class InstructionLedger:
    """Remembers instructions that already ran, so a resend cannot repeat them.

    Delegating a Project is not free: a duplicated instruction sends the Agent
    the same task twice, and the Agent may act on the outside world.  A caller
    that supplies a ``request_id`` gets exactly-once handling — the second
    delivery replays the recorded answer instead of routing again.

    Same idea as ``process_activations`` for Process activations: the ledger is
    the guarantee, not the caller's good behaviour.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def key(source: str, origin_project_id: str | None, request_id: str) -> str:
        """Build the ledger key for one instruction delivery."""
        return f"{source}:{origin_project_id or '-'}:{request_id}"

    def get(self, instruction_key: str) -> dict[str, Any] | None:
        """Return the recorded outcome for ``instruction_key``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM orchestrator_instructions WHERE instruction_key = ?",
            (instruction_key,),
        )
        if row is None:
            return None
        return {
            "instruction_key": row["instruction_key"],
            "origin_project_id": row["origin_project_id"],
            "request_id": row["request_id"],
            "source": row["source"],
            "message": row["message"],
            "decision": loads(row["decision"]) or {},
            "affected_project_id": row["affected_project_id"],
            "created_at": row["created_at"],
        }

    def record(
        self,
        instruction_key: str,
        *,
        origin_project_id: str | None,
        request_id: str,
        source: str,
        message: str,
        decision: dict[str, Any],
        affected_project_id: str | None,
    ) -> bool:
        """Record one handled instruction.  ``False`` means it was already there."""
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO orchestrator_instructions
                (instruction_key, origin_project_id, request_id, source, message,
                 decision, affected_project_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instruction_key,
                origin_project_id,
                request_id,
                source,
                message,
                dumps(decision),
                affected_project_id,
                utcnow().isoformat(),
            ),
        )
        return cursor.rowcount > 0

    def for_project(self, origin_project_id: str) -> list[dict[str, Any]]:
        """Return the instructions recorded against one project, oldest first."""
        rows = self.db.query(
            "SELECT instruction_key FROM orchestrator_instructions "
            "WHERE origin_project_id = ? ORDER BY created_at ASC",
            (origin_project_id,),
        )
        return [self.get(row["instruction_key"]) for row in rows]


class A2AMessageStore:
    """Append-only audit of the NEXUS SEED <-> Project Agent channel."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, message: A2AMessage, *, direction: str) -> A2AMessage:
        """Record one message; ``direction`` is ``"inbound"`` or ``"outbound"``."""
        self.append_new(message, direction=direction)
        return message

    def append_new(self, message: A2AMessage, *, direction: str) -> bool:
        """Record one message and report whether this id was new."""
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO orchestrator_a2a_messages
                (id, source_agent_id, project_id, direction, type, payload, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.source_agent_id,
                message.project_id,
                direction,
                message.type.value,
                dumps(message.payload),
                message.timestamp.isoformat(),
            ),
        )
        return cursor.rowcount > 0

    def for_project(self, project_id: str) -> list[tuple[str, A2AMessage]]:
        """Return ``(direction, message)`` pairs for ``project_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM orchestrator_a2a_messages WHERE project_id = ? "
            "ORDER BY timestamp ASC",
            (project_id,),
        )
        return [(r["direction"], self._row(r)) for r in rows]

    def all(self) -> list[tuple[str, A2AMessage]]:
        """Return every recorded message as ``(direction, message)``."""
        rows = self.db.query("SELECT * FROM orchestrator_a2a_messages ORDER BY timestamp ASC")
        return [(r["direction"], self._row(r)) for r in rows]

    @staticmethod
    def _row(row) -> A2AMessage:
        return A2AMessage(
            type=A2AMessageType(row["type"]),
            project_id=row["project_id"],
            source_agent_id=row["source_agent_id"],
            payload=loads(row["payload"]) or {},
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
        )


__all__ = ["A2AMessageStore", "AgentStore", "InstructionLedger", "ProjectStore"]
