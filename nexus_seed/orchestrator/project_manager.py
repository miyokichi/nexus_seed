"""ProjectManager — Project CRUD, status, priority, relationships, lifecycle.

This is the only component that changes a Project record.  It owns *management*
state (what the project is, how urgent, who has it, what it is waiting on) and
deliberately not execution state: the internal task breakdown, plans and tool
calls belong to the Project Agent.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..core.event import utcnow
from ..storage.orchestrator_store import ProjectStore
from .models import (
    LIVE_STATUSES,
    TERMINAL_STATUSES,
    Project,
    ProjectStatus,
)

logger = logging.getLogger("nexus_seed.orchestrator.project_manager")


class ProjectManager:
    """Creates and steers Projects."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    # --- creation ----------------------------------------------------------

    def create(
        self,
        goal: str,
        *,
        context: dict[str, Any] | None = None,
        priority: int = 0,
        parent_project_id: str | None = None,
    ) -> Project:
        """Create a Project for ``goal`` in status ``CREATED``."""
        project = Project(
            goal=goal,
            context=dict(context or {}),
            priority=priority,
            parent_project_id=parent_project_id,
        )
        self.store.save(project)
        logger.info(
            "created project %s (parent=%s): %s", project.id, parent_project_id, goal
        )
        return project

    # --- reads -------------------------------------------------------------

    def get(self, project_id: str) -> Project | None:
        """Return a project by id."""
        return self.store.get(project_id)

    def all(self) -> list[Project]:
        """Return every project."""
        return self.store.all()

    def live(self) -> list[Project]:
        """Return the projects that still need an Agent, most urgent first."""
        return self.store.by_status(*LIVE_STATUSES)

    def children_of(self, project_id: str) -> list[Project]:
        """Return the child projects of ``project_id``."""
        return self.store.children_of(project_id)

    # --- management state --------------------------------------------------

    def set_status(self, project: Project, status: ProjectStatus) -> Project:
        """Move a project to ``status``."""
        if project.status is status:
            return project
        logger.info("project %s: %s -> %s", project.id, project.status.value, status.value)
        project.status = status
        if status in TERMINAL_STATUSES:
            project.blockers = []
        return self.store.save(project)

    def assign_agent(self, project: Project, agent_id: str) -> Project:
        """Record which Agent owns this project."""
        project.assigned_agent_id = agent_id
        return self.store.save(project)

    def set_priority(self, project: Project, priority: int) -> Project:
        """Change a project's priority."""
        project.priority = priority
        return self.store.save(project)

    def set_summary(self, project: Project, summary: str) -> Project:
        """Replace the project's latest progress summary."""
        project.summary = summary
        return self.store.save(project)

    def add_task(self, project: Project, description: str) -> dict[str, Any]:
        """Attach a Task to an existing project and return the task record.

        NEXUS SEED records that the Task belongs to this project; the Agent
        decides how to break it down and carry it out.
        """
        task = {
            "id": str(uuid.uuid4()),
            "description": description,
            "created_at": utcnow().isoformat(),
        }
        project.tasks = [*project.tasks, task]
        self.store.save(project)
        logger.info("project %s: added task %s", project.id, task["id"])
        return task

    def add_blocker(
        self, project: Project, *, kind: str, reason: str, detail: dict[str, Any] | None = None
    ) -> Project:
        """Record why a project cannot currently proceed."""
        project.blockers = [
            *project.blockers,
            {
                "kind": kind,
                "reason": reason,
                "detail": dict(detail or {}),
                "raised_at": utcnow().isoformat(),
            },
        ]
        return self.store.save(project)

    def clear_blockers(self, project: Project) -> Project:
        """Drop all recorded blockers (the project can proceed again)."""
        project.blockers = []
        return self.store.save(project)

    # --- lifecycle shorthands ---------------------------------------------

    def activate(self, project: Project) -> Project:
        """Mark a project as being worked on by its Agent."""
        return self.set_status(project, ProjectStatus.ACTIVE)

    def block(
        self, project: Project, *, kind: str, reason: str, detail: dict[str, Any] | None = None
    ) -> Project:
        """Block a project and record why."""
        self.add_blocker(project, kind=kind, reason=reason, detail=detail)
        return self.set_status(project, ProjectStatus.BLOCKED)

    def wait_for_human(
        self, project: Project, *, reason: str, detail: dict[str, Any] | None = None
    ) -> Project:
        """Park a project until a person answers."""
        self.add_blocker(project, kind="NEED_HUMAN_INPUT", reason=reason, detail=detail)
        return self.set_status(project, ProjectStatus.WAITING_HUMAN)

    def complete(self, project: Project, *, summary: str | None = None) -> Project:
        """Finish a project successfully."""
        if summary is not None:
            project.summary = summary
        return self.set_status(project, ProjectStatus.COMPLETED)

    def fail(self, project: Project, *, reason: str) -> Project:
        """End a project that cannot be completed."""
        project.summary = reason
        return self.set_status(project, ProjectStatus.FAILED)

    def cancel(self, project: Project) -> Project:
        """Cancel a project."""
        return self.set_status(project, ProjectStatus.CANCELLED)


__all__ = ["ProjectManager"]
