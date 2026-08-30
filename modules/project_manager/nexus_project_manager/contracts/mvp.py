"""Stable data exchanged by the small, synchronous MVP application loop.

These records are application-domain data.  They do not add to or replace the
six NEXUS SEED Core primitives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TypeAlias

from nexus_project_manager._support.core.event import utcnow

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


@dataclass(frozen=True, slots=True)
class Observation:
    """One uninterpreted item seen by an MVP Observer."""

    content: JsonValue
    source: str
    id: str = field(default_factory=lambda: _new_id("observation"))
    observed_at: datetime = field(default_factory=utcnow)
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    """The storage-neutral form exposed by the Knowledge Gateway."""

    content: JsonValue
    source: str
    id: str = field(default_factory=lambda: _new_id("knowledge"))
    created_at: datetime = field(default_factory=utcnow)
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProjectProposal:
    """A Planner recommendation; it is not yet an executable Project."""

    title: str
    goal: str
    reason: str
    context: JsonObject = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("proposal"))


class ProjectStatus(str, Enum):
    """The deliberately small lifecycle exposed by the MVP boundary."""

    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ProjectResultStatus(str, Enum):
    """Possible outcomes returned by a Project Executor."""

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProjectExecutionRequest:
    """Everything an Executor may use to work on one approved Project."""

    project_id: str
    goal: str
    context: JsonObject = field(default_factory=dict)
    constraints: JsonValue = None
    workspace: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectResult:
    """The storage- and executor-neutral outcome of Project execution."""

    project_id: str
    status: ProjectResultStatus
    summary: str
    outputs: JsonObject = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        """Normalize string input while keeping the public JSON-like API convenient."""

        if not isinstance(self.status, ProjectResultStatus):
            object.__setattr__(self, "status", ProjectResultStatus(self.status))


@dataclass(frozen=True, slots=True)
class Project:
    """The Project Manager's public view of managed state."""

    id: str
    title: str
    goal: str
    context: JsonObject
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    result: ProjectResult | None = None

    def __post_init__(self) -> None:
        """Normalize string input from lightweight third-party managers."""

        if not isinstance(self.status, ProjectStatus):
            object.__setattr__(self, "status", ProjectStatus(self.status))


@dataclass(frozen=True, slots=True)
class MVPRunReport:
    """A compact account of one complete MVP loop invocation."""

    observations: tuple[Observation, ...] = ()
    proposals: tuple[ProjectProposal, ...] = ()
    projects: tuple[Project, ...] = ()
    results: tuple[ProjectResult, ...] = ()
    rejected_proposal_ids: tuple[str, ...] = ()


def to_knowledge_item(item: Observation | ProjectResult) -> KnowledgeItem:
    """Normalize an MVP observation or result before it crosses the Gateway."""

    if isinstance(item, Observation):
        return KnowledgeItem(
            id=item.id,
            content=item.content,
            source=item.source,
            created_at=item.observed_at,
            metadata=dict(item.metadata),
        )
    return KnowledgeItem(
        id=f"result-{item.project_id}",
        content={
            "project_id": item.project_id,
            "status": item.status.value,
            "summary": item.summary,
            "outputs": item.outputs,
            "error": item.error,
        },
        source="project_executor",
        metadata={"project_id": item.project_id},
    )


__all__ = [
    "KnowledgeItem",
    "JsonObject",
    "JsonValue",
    "MVPRunReport",
    "Observation",
    "Project",
    "ProjectExecutionRequest",
    "ProjectProposal",
    "ProjectResult",
    "ProjectResultStatus",
    "ProjectStatus",
    "to_knowledge_item",
]
