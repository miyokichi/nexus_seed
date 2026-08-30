"""Project lifecycle, assignment, workspace, and A2A communication module."""

from .a2a_gateway import A2AGateway
from .adapters.mvp import ExistingProjectManagerAdapter
from .agent_manager import AgentManager
from .agent_runtime import (
    A2AAgentRuntime,
    AgentRuntime,
    AgentUnavailable,
    Dispatch,
    InProcessAgentRuntime,
    ProjectAgentTransport,
    RemoteWorkLost,
    completed_message,
    escalation,
    status_message,
)
from .context_manager import ContextManager
from .models import *  # noqa: F403
from .models import __all__ as _MODEL_EXPORTS
from .orchestrator import ProjectOrchestrator
from .project_manager import ProjectManager
from .router import ProjectRouter

__all__ = [
    *_MODEL_EXPORTS,
    "A2AAgentRuntime",
    "A2AGateway",
    "AgentManager",
    "AgentRuntime",
    "AgentUnavailable",
    "ContextManager",
    "Dispatch",
    "ExistingProjectManagerAdapter",
    "InProcessAgentRuntime",
    "ProjectAgentTransport",
    "ProjectManager",
    "ProjectOrchestrator",
    "ProjectRouter",
    "RemoteWorkLost",
    "completed_message",
    "escalation",
    "status_message",
]
