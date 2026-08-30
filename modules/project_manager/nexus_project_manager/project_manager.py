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

from nexus_project_manager._support.core.event import utcnow
from .adapters.sqlite import ProjectStore
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
            # Nothing is in the way of a finished Project, but that something
            # once was stays in its history.
            self._resolve(project, by=f"project {status.value.lower()}")
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

    def set_context(self, project: Project, context: dict[str, Any]) -> Project:
        """Replace what is known about the project.

        The context travels with every delegation and survives a restart, so
        this is also where durable per-project decisions live — what the task
        has been granted, most notably.
        """
        project.context = dict(context)
        return self.store.save(project)

    def add_task(
        self,
        project: Project,
        description: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Attach a Task to an existing project and return the task record.

        NEXUS SEED records that the Task belongs to this project; the Agent
        decides how to break it down and carry it out.  ``context`` carries
        evidence or other provenance that applies to this Task rather than to
        the Project's goal as a whole.
        """
        task = {
            "id": str(uuid.uuid4()),
            "description": description,
            "created_at": utcnow().isoformat(),
        }
        if context:
            task["context"] = dict(context)
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

    def resolve_blockers(self, project: Project, *, by: str) -> Project:
        """Mark what was in the way as no longer in the way, and keep it.

        Blockers are audit, not just control state: "SAP access was missing,
        and then a person said to proceed without it" is the interesting part,
        so resolving records ``by`` and ``resolved_at`` rather than deleting.
        """
        resolved = self._resolve(project, by=by)
        if resolved:
            logger.info("project %s: %d blocker(s) resolved by %s", project.id, resolved, by)
        return self.store.save(project)

    def clear_blockers(self, project: Project) -> Project:
        """Mark every current blocker resolved (the project can proceed again)."""
        return self.resolve_blockers(project, by="resolved")

    @staticmethod
    def _resolve(project: Project, *, by: str) -> int:
        """Stamp every current blocker as resolved; return how many there were."""
        now = utcnow().isoformat()
        resolved = 0
        blockers = []
        for blocker in project.blockers:
            if not blocker.get("resolved_at"):
                blocker = {**blocker, "resolved_at": now, "resolved_by": by}
                resolved += 1
            blockers.append(blocker)
        project.blockers = blockers
        return resolved

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

    def wait_for_completion_review(
        self,
        project: Project,
        *,
        message_id: str,
        summary: str,
        detail: dict[str, Any] | None = None,
    ) -> Project:
        """Park an Agent completion until its deliverables are reviewed.

        The current review marker lives with the Project so recovery does not
        depend on a Python call stack.  Replaying the same A2A message is a
        no-op; a later completion attempt archives the previous marker.
        """
        current = project.context.get("completion_review")
        if isinstance(current, dict) and current.get("message_id") == message_id:
            return project
        context = dict(project.context)
        if isinstance(current, dict):
            context["completion_review_history"] = [
                *list(context.get("completion_review_history") or []),
                dict(current),
            ]
        context["completion_review"] = {
            "message_id": message_id,
            "status": "PENDING_REVIEW",
            "summary": summary,
            "detail": dict(detail or {}),
            "requested_at": utcnow().isoformat(),
        }
        project.context = context
        project.summary = summary
        self.add_blocker(
            project,
            kind="COMPLETION_REVIEW",
            reason="Agentの成果物と完了報告の確認が必要です",
            detail={"message_id": message_id, **dict(detail or {})},
        )
        return self.set_status(project, ProjectStatus.WAITING_REVIEW)

    def decide_completion_review(
        self,
        project: Project,
        *,
        message_id: str,
        decision: str,
        actor: str,
        note: str = "",
    ) -> bool:
        """Record one completion decision; return whether it changed state."""
        current = project.context.get("completion_review")
        if (
            not isinstance(current, dict)
            or current.get("message_id") != message_id
            or current.get("status") != "PENDING_REVIEW"
        ):
            return False
        context = dict(project.context)
        context["completion_review"] = {
            **current,
            "status": decision,
            "reviewed_by": actor,
            "review_note": note,
            "reviewed_at": utcnow().isoformat(),
        }
        project.context = context
        self.store.save(project)
        return True

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
