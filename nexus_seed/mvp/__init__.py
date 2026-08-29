"""Minimal replaceable Observer-to-Project autonomous application loop."""

from .approval import CLIHumanApproval, FixedApproval
from .executor import DefaultProjectExecutor
from .interfaces import (
    HumanApproval,
    KnowledgeGateway,
    LLMProvider,
    Observer,
    ProjectExecutor,
    ProjectManager,
    ProjectPlanner,
)
from .knowledge import ExistingKnowledgeGateway
from .llm import ExistingBackendLLMProvider
from .models import (
    KnowledgeItem,
    JsonObject,
    JsonValue,
    MVPRunReport,
    Observation,
    Project,
    ProjectExecutionRequest,
    ProjectProposal,
    ProjectResult,
    ProjectResultStatus,
    ProjectStatus,
    to_knowledge_item,
)
from .observer import ExistingIngressObserver, ManualIngressObserver, TextObserver
from .planner import SimpleProjectPlanner
from .project import ExistingProjectManagerAdapter
from .runtime import MVPApplicationRuntime

__all__ = [
    "CLIHumanApproval",
    "DefaultProjectExecutor",
    "ExistingBackendLLMProvider",
    "ExistingKnowledgeGateway",
    "ExistingIngressObserver",
    "ExistingProjectManagerAdapter",
    "FixedApproval",
    "HumanApproval",
    "KnowledgeGateway",
    "KnowledgeItem",
    "JsonObject",
    "JsonValue",
    "LLMProvider",
    "MVPApplicationRuntime",
    "MVPRunReport",
    "ManualIngressObserver",
    "Observation",
    "Observer",
    "Project",
    "ProjectExecutionRequest",
    "ProjectExecutor",
    "ProjectManager",
    "ProjectPlanner",
    "ProjectProposal",
    "ProjectResult",
    "ProjectResultStatus",
    "ProjectStatus",
    "SimpleProjectPlanner",
    "TextObserver",
    "to_knowledge_item",
]
