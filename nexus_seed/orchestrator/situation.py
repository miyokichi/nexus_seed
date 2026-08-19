"""What is happening in one orchestrator Project, in the shape people read.

A Goal-rooted project is reconstructed from Work, Processes and Reviews because
NEXUS SEED did the work itself.  An orchestrator Project is not: the Agent owns
the execution, so the honest account is what NEXUS SEED actually knows — the
goal it delegated, the Tasks it handed over, what the Agent reported back, what
is in the way, and when.

The result is a :class:`~nexus_seed.projects.models.ProjectSituation` so that
Project Chat, ``GET /projects/{id}/situation`` and the Cockpit read an
orchestrator Project through exactly the same surface as a Goal-rooted one.
When Goals are gone this stays; the other source is what goes away.
"""

from __future__ import annotations

from typing import Any

from ..projects.models import ProjectOverallStatus, ProjectSituation
from ..storage.orchestrator_store import A2AMessageStore, AgentStore, ProjectStore
from .models import Project, ProjectStatus


#: How much of a Project's A2A history a person is shown by default.
RECENT_LIMIT = 20

#: Project lifecycle mapped onto the status vocabulary the interface speaks.
#: ``FAILED`` becomes NEEDS_ATTENTION rather than a status of its own: a failed
#: Project is precisely a Project that needs a person to look at it.
_STATUS = {
    ProjectStatus.CREATED: ProjectOverallStatus.PLANNING,
    ProjectStatus.ACTIVE: ProjectOverallStatus.ACTIVE,
    ProjectStatus.BLOCKED: ProjectOverallStatus.BLOCKED,
    ProjectStatus.WAITING_HUMAN: ProjectOverallStatus.NEEDS_ATTENTION,
    ProjectStatus.COMPLETED: ProjectOverallStatus.COMPLETED,
    ProjectStatus.FAILED: ProjectOverallStatus.NEEDS_ATTENTION,
    ProjectStatus.CANCELLED: ProjectOverallStatus.CANCELLED,
}


def orchestrator_situation(
    runtime, project_id: str, *, recent_limit: int = RECENT_LIMIT
) -> ProjectSituation | None:
    """Return one orchestrator Project as a situation, or ``None`` if unknown."""

    project = ProjectStore(runtime.db).get(str(project_id))
    if project is None:
        return None
    return _compile(runtime, project, recent_limit)


def orchestrator_situations(
    runtime, *, recent_limit: int = RECENT_LIMIT
) -> list[ProjectSituation]:
    """Return every orchestrator Project as a situation, newest activity last."""

    projects = sorted(ProjectStore(runtime.db).all(), key=lambda item: item.id)
    return [_compile(runtime, project, recent_limit) for project in projects]


def _compile(runtime, project: Project, recent_limit: int) -> ProjectSituation:
    agent = AgentStore(runtime.db).active_for_project(project.id)
    messages = A2AMessageStore(runtime.db).for_project(project.id)
    finished = project.status is ProjectStatus.COMPLETED
    tasks = tuple(_task(task, finished=finished) for task in project.tasks)
    blocked = project.status is ProjectStatus.BLOCKED
    return ProjectSituation(
        project_id=project.id,
        title=_title(project.goal),
        objective=project.goal,
        overall_status=_STATUS.get(project.status, ProjectOverallStatus.IDLE),
        summary=project.summary or _default_summary(project, agent),
        # NEXUS SEED does not track a Task's progress — the Agent does — so the
        # split is by the *Project's* state rather than invented per Task.
        active_work=() if finished or blocked else tasks,
        blocked_work=tasks if blocked else (),
        recently_completed_work=tasks if finished else (),
        completed_total=len(tasks) if finished else 0,
        blockers=tuple(_blocker(item) for item in project.current_blockers),
        recent_changes=tuple(
            _change(direction, message) for direction, message in messages
        )[-recent_limit:],
        recent_events=tuple(
            {"type": f"a2a.{message.type.value}", "occurred_at": _iso(message.timestamp)}
            for _direction, message in messages
        )[-recent_limit:],
        updated_at=project.updated_at,
    )


def _title(goal: str) -> str:
    """A one-line name for a Project whose goal may be a paragraph."""

    first = (goal or "").strip().splitlines()[0] if (goal or "").strip() else ""
    return first if len(first) <= 60 else first[:59].rstrip() + "…"


def _default_summary(project: Project, agent) -> str:
    """Say something true when the Agent has not reported yet."""

    if agent is None:
        return "まだAgentに委譲されていません。"
    return f"{agent.agent_id} に委譲済みです。Agentからの報告はまだありません。"


def _task(task: dict[str, Any], *, finished: bool) -> dict[str, Any]:
    return {
        "id": str(task.get("id") or ""),
        "type": "task",
        "objective": str(task.get("description") or ""),
        "status": "COMPLETED" if finished else "DELEGATED",
        "updated_at": task.get("created_at"),
    }


def _blocker(blocker: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": str(blocker.get("kind") or "blocker"),
        "severity": "BLOCKING",
        "summary": str(blocker.get("reason") or ""),
        "occurred_at": blocker.get("raised_at"),
    }


def _change(direction: str, message) -> dict[str, Any]:
    return {
        "type": f"a2a.{message.type.value}",
        "id": message.id,
        "summary": f"{direction} {message.type.value}: {_message_text(message)}",
        "status": direction,
        "occurred_at": _iso(message.timestamp),
    }


def _message_text(message) -> str:
    """The most human line an A2A payload carries, whatever shape it is in."""

    payload = message.payload or {}
    for key in ("summary", "reason", "message", "note", "description", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "RECENT_LIMIT",
    "orchestrator_situation",
    "orchestrator_situations",
]
