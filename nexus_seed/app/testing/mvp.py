"""Small test doubles proving every MVP boundary is replaceable."""

from __future__ import annotations

from dataclasses import replace
from ...core.event import utcnow
from ...platform.contracts.mvp import (
    JsonObject,
    JsonValue,
    KnowledgeItem,
    Observation,
    Project,
    ProjectExecutionRequest,
    ProjectProposal,
    ProjectResult,
    ProjectResultStatus,
    ProjectStatus,
)


class MockObserver:
    """Return a scripted batch of observations."""

    def __init__(self, observations: list[Observation]) -> None:
        self.observations = list(observations)
        self.calls = 0

    def observe(self) -> list[Observation]:
        self.calls += 1
        return list(self.observations)


class MockKnowledgeGateway:
    """Keep MVP items in memory while recording calls."""

    def __init__(self) -> None:
        self.items: dict[str, KnowledgeItem] = {}
        self.put_calls: list[KnowledgeItem] = []

    def put(self, item: KnowledgeItem) -> None:
        self.put_calls.append(item)
        self.items[item.id] = item

    def get(self, item_id: str) -> KnowledgeItem | None:
        return self.items.get(item_id)

    def search(self, query: str) -> list[KnowledgeItem]:
        needle = query.casefold()
        return [item for item in self.items.values() if needle in str(item.content).casefold()]


class MockProjectPlanner:
    """Return scripted proposals and retain the contexts it saw."""

    def __init__(self, proposals: list[ProjectProposal]) -> None:
        self.proposals = list(proposals)
        self.calls: list[KnowledgeItem] = []

    def propose(self, context: KnowledgeItem) -> list[ProjectProposal]:
        self.calls.append(context)
        return list(self.proposals)


class MockProjectManager:
    """In-memory lifecycle implementation with the production interface."""

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}

    def create(self, proposal: ProjectProposal) -> Project:
        now = utcnow()
        project = Project(
            id=f"project-{proposal.id}",
            title=proposal.title,
            goal=proposal.goal,
            context=dict(proposal.context),
            status=ProjectStatus.READY,
            created_at=now,
            updated_at=now,
        )
        self.projects[project.id] = project
        return project

    def get(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def list(self) -> list[Project]:
        return list(self.projects.values())

    def mark_running(self, project_id: str) -> Project:
        return self._status(project_id, ProjectStatus.RUNNING)

    def mark_completed(self, project_id: str, result: ProjectResult) -> Project:
        project = self._status(project_id, ProjectStatus.COMPLETED, result=result)
        return project

    def mark_failed(self, project_id: str, reason: str) -> Project:
        result = ProjectResult(
            project_id=project_id,
            status=ProjectResultStatus.FAILED,
            summary=reason,
            error=reason,
        )
        return self._status(project_id, ProjectStatus.FAILED, result=result)

    def _status(
        self,
        project_id: str,
        status: ProjectStatus,
        *,
        result: ProjectResult | None = None,
    ) -> Project:
        current = self.projects[project_id]
        project = replace(current, status=status, result=result, updated_at=utcnow())
        self.projects[project_id] = project
        return project


class MockProjectExecutor:
    """Return a scripted terminal result for each execution request."""

    def __init__(self, result: ProjectResult | None = None) -> None:
        self.result = result
        self.calls: list[ProjectExecutionRequest] = []

    def execute(self, request: ProjectExecutionRequest) -> ProjectResult:
        self.calls.append(request)
        if self.result is None:
            return ProjectResult(
                project_id=request.project_id,
                status=ProjectResultStatus.COMPLETED,
                summary="Project completed",
            )
        return replace(self.result, project_id=request.project_id)


class MockLLMProvider:
    """Replay provider outputs without network access."""

    def __init__(
        self,
        responses: list[JsonValue] | None = None,
        *,
        default: JsonValue = None,
    ) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.calls: list[tuple[list[JsonObject], JsonObject | None]] = []

    def generate(
        self,
        messages: list[JsonObject],
        schema: JsonObject | None = None,
    ) -> JsonValue:
        self.calls.append((messages, schema))
        if self.responses:
            index = min(len(self.calls) - 1, len(self.responses) - 1)
            return self.responses[index]
        return self.default


__all__ = [
    "MockKnowledgeGateway",
    "MockLLMProvider",
    "MockObserver",
    "MockProjectExecutor",
    "MockProjectManager",
    "MockProjectPlanner",
]
