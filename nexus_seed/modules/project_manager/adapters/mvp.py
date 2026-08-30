"""MVP Project Manager adapter over the existing durable Project records."""

from __future__ import annotations

from ..models import Project as ExistingProject
from ..models import ProjectStatus as ExistingStatus
from ..project_manager import ProjectManager as ExistingProjectManager
from ....platform.contracts.mvp import (
    Project,
    JsonObject,
    ProjectProposal,
    ProjectResult,
    ProjectResultStatus,
    ProjectStatus,
)

_MVP_KEY = "_nexus_seed_mvp"


class ExistingProjectManagerAdapter:
    """Keep MVP lifecycle state in the existing SQLite Project store."""

    def __init__(self, manager: ExistingProjectManager) -> None:
        self.manager = manager

    def create(self, proposal: ProjectProposal) -> Project:
        """Create a durable READY Project from an approved proposal."""

        context = dict(proposal.context)
        context[_MVP_KEY] = {
            "proposal_id": proposal.id,
            "title": proposal.title,
            "reason": proposal.reason,
        }
        return _to_mvp(self.manager.create(proposal.goal, context=context))

    def get(self, project_id: str) -> Project | None:
        """Return a durable Project by id."""

        project = self.manager.get(project_id)
        return _to_mvp(project) if project and _is_mvp(project) else None

    def list(self) -> list[Project]:
        """Return every durable Project, oldest first."""

        return [
            _to_mvp(project)
            for project in self.manager.all()
            if _is_mvp(project)
        ]

    def mark_running(self, project_id: str) -> Project:
        """Move a durable Project to RUNNING."""

        return _to_mvp(self.manager.activate(self._required(project_id)))

    def mark_completed(self, project_id: str, result: ProjectResult) -> Project:
        """Atomically save the result and move the Project to COMPLETED."""

        if result.project_id != project_id:
            raise ValueError("result.project_id does not match project_id")
        if result.status != ProjectResultStatus.COMPLETED:
            raise ValueError("a non-completed result cannot complete a project")
        project = self._required(project_id)
        with self.manager.store.db.atomic():
            self._store_result(project, result)
            project = self.manager.complete(project, summary=result.summary)
        return _to_mvp(project)

    def mark_failed(self, project_id: str, reason: str) -> Project:
        """Atomically save a failure result and move the Project to FAILED."""

        project = self._required(project_id)
        result = ProjectResult(
            project_id=project_id,
            status=ProjectResultStatus.FAILED,
            summary=reason,
            error=reason,
        )
        with self.manager.store.db.atomic():
            self._store_result(project, result)
            project = self.manager.fail(project, reason=reason)
        return _to_mvp(project)

    def _required(self, project_id: str) -> ExistingProject:
        project = self.manager.get(project_id)
        if project is None or not _is_mvp(project):
            raise KeyError(f"unknown project {project_id!r}")
        return project

    def _store_result(self, project: ExistingProject, result: ProjectResult) -> None:
        context = dict(project.context)
        mvp = dict(context.get(_MVP_KEY) or {})
        mvp["result"] = _result_dict(result)
        context[_MVP_KEY] = mvp
        self.manager.set_context(project, context)


def _to_mvp(project: ExistingProject) -> Project:
    internal = dict(project.context.get(_MVP_KEY) or {})
    context = {key: value for key, value in project.context.items() if key != _MVP_KEY}
    raw_result = internal.get("result")
    return Project(
        id=project.id,
        title=str(internal.get("title") or project.goal),
        goal=project.goal,
        context=context,
        status=_status(project.status),
        created_at=project.created_at,
        updated_at=project.updated_at,
        result=_result(raw_result) if isinstance(raw_result, dict) else None,
    )


def _is_mvp(project: ExistingProject) -> bool:
    internal = project.context.get(_MVP_KEY)
    return isinstance(internal, dict) and bool(internal.get("proposal_id"))


def _status(status: ExistingStatus) -> ProjectStatus:
    if status is ExistingStatus.CREATED:
        return ProjectStatus.READY
    if status is ExistingStatus.ACTIVE:
        return ProjectStatus.RUNNING
    if status is ExistingStatus.COMPLETED:
        return ProjectStatus.COMPLETED
    if status is ExistingStatus.FAILED:
        return ProjectStatus.FAILED
    raise ValueError(
        f"MVP project has unsupported existing status {status.value!r}; "
        "it will not be represented as FAILED"
    )


def _result_dict(result: ProjectResult) -> JsonObject:
    return {
        "project_id": result.project_id,
        "status": result.status.value,
        "summary": result.summary,
        "outputs": dict(result.outputs),
        "error": result.error,
    }


def _result(value: JsonObject) -> ProjectResult:
    return ProjectResult(
        project_id=str(value["project_id"]),
        status=ProjectResultStatus(value["status"]),
        summary=str(value.get("summary") or ""),
        outputs=dict(value.get("outputs") or {}),
        error=value.get("error"),
    )


__all__ = ["ExistingProjectManagerAdapter"]
