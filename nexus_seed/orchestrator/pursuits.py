"""Projects are what NEXUS SEED is pursuing.

A Project *is* a goal — the objective a person handed over, delegated whole to
one Agent — so Phase 6 holds its Intention about the Project directly.  There is
nothing between the two to decompose: the Agent does that inside the Project.
"""

from __future__ import annotations

from ..pursuit import Pursuit, PursuitSource
from ..storage.orchestrator_store import ProjectStore
from .models import Project


class ProjectPursuits(PursuitSource):
    """Answer "what are we pursuing?" with the Project Orchestrator's Projects."""

    def __init__(self, projects: ProjectStore) -> None:
        self.projects = projects

    def live(self) -> list[Pursuit]:
        return [
            self.pursuit(project)
            for project in sorted(self.projects.all(), key=lambda item: item.id)
            if project.is_live
        ]

    def get(self, pursuit_id: str) -> Pursuit | None:
        project = self.projects.get(str(pursuit_id))
        return self.pursuit(project) if project is not None else None

    @staticmethod
    def pursuit(project: Project) -> Pursuit:
        """One Project as the thing being pursued.

        ``reconsider_on`` comes from the Project's context because that is
        where a caller can put it; a Project that names none is simply never
        reconsidered on an event type.
        """
        raw = project.context.get("reconsider_on") or ()
        return Pursuit(
            id=project.id,
            objective=project.goal,
            title=project.summary,
            active=project.is_live,
            reconsider_on=tuple(str(value) for value in raw),
            source={"kind": "project", "status": project.status.value},
        )


__all__ = ["ProjectPursuits"]
