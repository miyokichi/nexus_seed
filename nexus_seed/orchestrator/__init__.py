"""NEXUS SEED as a Project Orchestrator.

    ContextManager -> ProjectRouter -> ProjectManager -> AgentManager -> A2AGateway

One Project = one Agent.  NEXUS SEED decides which projects exist and steers
them; the Project Agent decides how its Goal is actually carried out and only
comes back when it cannot continue alone.
"""

from .a2a_gateway import A2AGateway
from .agent_manager import AgentManager
from .agent_runtime import (
    A2AAgentRuntime,
    AgentRuntime,
    InProcessAgentRuntime,
    completed_message,
    escalation,
    status_message,
)
from .context_manager import ContextManager
from .models import (
    A2AMessage,
    A2AMessageType,
    Agent,
    AgentStatus,
    Project,
    ProjectAgentConfig,
    ProjectStatus,
    RoutingAction,
    RoutingContext,
    RoutingDecision,
)
from .orchestrator import ProjectOrchestrator
from .project_manager import ProjectManager
from .router import ProjectRouter

__all__ = [
    "A2AAgentRuntime",
    "A2AGateway",
    "A2AMessage",
    "A2AMessageType",
    "Agent",
    "AgentManager",
    "AgentRuntime",
    "AgentStatus",
    "ContextManager",
    "InProcessAgentRuntime",
    "Project",
    "ProjectAgentConfig",
    "ProjectManager",
    "ProjectOrchestrator",
    "ProjectRouter",
    "ProjectStatus",
    "RoutingAction",
    "RoutingContext",
    "RoutingDecision",
    "completed_message",
    "escalation",
    "status_message",
]
