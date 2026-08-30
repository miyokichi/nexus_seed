"""Persistence for project-scoped chat threads and messages.

This is an ordinary append-only journal in the existing SQLite database, in the
same spirit as ``llm_invocations``: it adds no Core primitive, no Runtime and
no write path into Event, World State, Goal or Work.  ``seq`` gives the thread
a stable replay order that does not depend on clock resolution.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..chat.models import (
    ChatAnswerStatus,
    ChatCertainty,
    ChatRole,
    ProjectChatMessage,
    ProjectChatThread,
)
from .database import Database, dumps, loads


class ProjectChatStore:
    """Stores one thread per project plus its ordered messages."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- threads -----------------------------------------------------------

    def get_thread(self, project_id: str) -> ProjectChatThread | None:
        """Return the thread of ``project_id``, or ``None`` when none exists."""

        row = self.db.query_one(
            "SELECT * FROM project_chat_threads WHERE project_id = ?", (project_id,)
        )
        return self._thread(row) if row else None

    def ensure_thread(self, project_id: str) -> ProjectChatThread:
        """Return the project's thread, creating it on first use."""

        existing = self.get_thread(project_id)
        if existing is not None:
            return existing
        thread = ProjectChatThread(project_id=project_id)
        self.db.execute(
            "INSERT INTO project_chat_threads (id, project_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                str(thread.id),
                thread.project_id,
                thread.created_at.isoformat(),
                thread.updated_at.isoformat(),
            ),
        )
        return thread

    def touch_thread(self, thread_id: uuid.UUID, updated_at: datetime) -> None:
        """Record when the thread last carried a message."""

        self.db.execute(
            "UPDATE project_chat_threads SET updated_at = ? WHERE id = ?",
            (updated_at.isoformat(), str(thread_id)),
        )

    # --- messages ----------------------------------------------------------

    def append(self, message: ProjectChatMessage) -> ProjectChatMessage:
        """Append one message and advance its thread's ``updated_at``."""

        self.db.execute(
            """
            INSERT INTO project_chat_messages
                (id, thread_id, project_id, role, text, status, certainty,
                 references_json, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(message.id),
                str(message.thread_id),
                message.project_id,
                message.role.value,
                message.text,
                message.status.value,
                message.certainty.value if message.certainty else None,
                dumps(list(message.references)),
                dumps(message.metadata),
                message.created_at.isoformat(),
            ),
        )
        self.touch_thread(message.thread_id, message.created_at)
        return message

    def messages(
        self, thread_id: uuid.UUID, *, limit: int | None = None
    ) -> list[ProjectChatMessage]:
        """Return a thread's messages oldest first, optionally the newest ``limit``."""

        rows = self.db.query(
            "SELECT * FROM project_chat_messages WHERE thread_id = ? ORDER BY seq ASC",
            (str(thread_id),),
        )
        selected = rows[-limit:] if limit is not None and limit >= 0 else rows
        return [self._message(row) for row in selected]

    def messages_for_project(
        self, project_id: str, *, limit: int | None = None
    ) -> list[ProjectChatMessage]:
        """Return one project's messages, so a caller never crosses projects."""

        thread = self.get_thread(project_id)
        return [] if thread is None else self.messages(thread.id, limit=limit)

    # --- rows --------------------------------------------------------------

    @staticmethod
    def _thread(row) -> ProjectChatThread:
        return ProjectChatThread(
            project_id=row["project_id"],
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _message(row) -> ProjectChatMessage:
        return ProjectChatMessage(
            thread_id=uuid.UUID(row["thread_id"]),
            project_id=row["project_id"],
            role=ChatRole(row["role"]),
            text=row["text"],
            status=ChatAnswerStatus(row["status"]),
            certainty=ChatCertainty(row["certainty"]) if row["certainty"] else None,
            references=tuple(loads(row["references_json"]) or ()),
            metadata=loads(row["metadata_json"]) or {},
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


__all__ = ["ProjectChatStore"]
