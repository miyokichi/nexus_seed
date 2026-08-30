"""Public, implementation-independent interfaces for the MVP modules."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    KnowledgeItem,
    JsonObject,
    JsonValue,
    Observation,
    Project,
    ProjectExecutionRequest,
    ProjectProposal,
    ProjectResult,
)


@runtime_checkable
class Observer(Protocol):
    """Turn authorized external input into observations, and nothing else."""

    def observe(self) -> list[Observation]:
        """Return observations currently available to the application."""
        ...


@runtime_checkable
class KnowledgeGateway(Protocol):
    """The only Knowledge API visible to other MVP modules."""

    def put(self, item: KnowledgeItem) -> None:
        """Persist one already-normalized Knowledge item."""
        ...

    def get(self, item_id: str) -> KnowledgeItem | None:
        """Return one current item by stable id."""
        ...

    def search(self, query: str) -> list[KnowledgeItem]:
        """Return current items matching a deterministic text query."""
        ...


@runtime_checkable
class ProjectPlanner(Protocol):
    """Decide whether context warrants one or more Project proposals."""

    def propose(self, context: KnowledgeItem) -> list[ProjectProposal]:
        """Return proposals only; never create or execute Projects."""
        ...


@runtime_checkable
class HumanApproval(Protocol):
    """Approval boundary between proposal and Project creation."""

    def approve(self, proposal: ProjectProposal) -> bool:
        """Return whether a person approved the proposal."""
        ...


@runtime_checkable
class ProjectManager(Protocol):
    """Own only the lifecycle state of MVP Projects."""

    def create(self, proposal: ProjectProposal) -> Project:
        """Create a READY Project from an approved proposal."""
        ...

    def get(self, project_id: str) -> Project | None:
        """Return a Project by id."""
        ...

    def list(self) -> list[Project]:
        """Return every Project, oldest first."""
        ...

    def mark_running(self, project_id: str) -> Project:
        """Move a Project to RUNNING."""
        ...

    def mark_completed(self, project_id: str, result: ProjectResult) -> Project:
        """Store the result and move a Project to COMPLETED."""
        ...

    def mark_failed(self, project_id: str, reason: str) -> Project:
        """Store the failure reason and move a Project to FAILED."""
        ...


@runtime_checkable
class ProjectExecutor(Protocol):
    """Execute an approved Project behind one replaceable boundary."""

    def execute(self, request: ProjectExecutionRequest) -> ProjectResult:
        """Execute the request and return a terminal result."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """Provider-neutral, synchronous generation API used by MVP modules."""

    def generate(
        self,
        messages: list[JsonObject],
        schema: JsonObject | None = None,
    ) -> JsonValue:
        """Generate a response, optionally constrained by ``schema``."""
        ...


__all__ = [
    "HumanApproval",
    "KnowledgeGateway",
    "LLMProvider",
    "Observer",
    "ProjectExecutor",
    "ProjectManager",
    "ProjectPlanner",
]
