"""A2A transport used by external Project Agents."""

from .a2a import (
    AGENT_CARD_PATH,
    A2AClient,
    A2AEndpoint,
    A2AProtocolError,
    task_state,
)
from .project_agent import (
    PROJECT_AGENT_INSTRUCTION,
    PROJECT_ASSIGNMENT,
    REPLY_SCHEMA,
    A2AProjectAgentTransport,
)

__all__ = [
    "AGENT_CARD_PATH",
    "A2AClient",
    "A2AEndpoint",
    "A2AProtocolError",
    "task_state",
    "A2AProjectAgentTransport",
    "PROJECT_AGENT_INSTRUCTION",
    "PROJECT_ASSIGNMENT",
    "REPLY_SCHEMA",
]
