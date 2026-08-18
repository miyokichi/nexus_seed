"""AgentRuntime — where a Project Agent actually runs.

NEXUS SEED does not contain an agent loop.  It starts a Project Agent somewhere
(in this process, as a subprocess, or as a remote A2A endpoint), hands it a
:class:`ProjectAgentConfig`, and afterwards talks to it only over A2A.

Two implementations ship:

* :class:`InProcessAgentRuntime` — a scripted, network-free runtime used by the
  tests, exactly as ``FakeLLMBackend`` is used for the LLM boundary.
* :class:`A2AAgentRuntime` — the seam for a real external Agent Runtime (Little
  Agent or anything else that answers A2A), reusing the existing
  ``providers/a2a.py`` boundary rather than adding a second protocol client.

Messages an Agent sends back are *queued*, not delivered re-entrantly: the
orchestrator drains them after the current step, the same way the Runtime drains
emitted events.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from .models import A2AMessage, A2AMessageType, ProjectAgentConfig

logger = logging.getLogger("nexus_seed.orchestrator.agent_runtime")


@runtime_checkable
class AgentRuntime(Protocol):
    """Somewhere a generic Project Agent can be started and talked to."""

    name: str

    async def spawn(self, config: ProjectAgentConfig) -> str:
        """Start an agent for ``config`` and return its endpoint (or ``""``)."""
        ...

    async def deliver(self, agent_id: str, envelope: dict[str, Any]) -> None:
        """Hand the agent a goal or an added task."""
        ...

    async def poll(self) -> list[A2AMessage]:
        """Return and clear the messages agents have sent to NEXUS SEED."""
        ...

    async def stop(self, agent_id: str) -> None:
        """Stop the agent."""
        ...

    async def health(self, agent_id: str) -> bool:
        """Whether the agent is still usable."""
        ...


#: A scripted agent behaviour: given its config and the delivered envelope, emit
#: the A2A messages that agent would send back.
Behaviour = Callable[[ProjectAgentConfig, dict[str, Any]], Awaitable[list[A2AMessage]] | list[A2AMessage]]


class InProcessAgentRuntime:
    """A deterministic, network-free Project Agent runtime for tests and demos.

    A ``behaviour`` decides what the agent reports back.  Nothing here reasons:
    it is the test's script, so the orchestration around it stays the thing
    under test.
    """

    name = "in_process"

    def __init__(self, behaviour: Behaviour | None = None) -> None:
        self.behaviour = behaviour
        self.configs: dict[str, ProjectAgentConfig] = {}
        self.delivered: list[tuple[str, dict[str, Any]]] = []
        self.stopped: list[str] = []
        self._outbox: list[A2AMessage] = []

    async def spawn(self, config: ProjectAgentConfig) -> str:
        """Register the agent; no process is started."""
        self.configs[config.agent_id] = config
        logger.info("in-process agent %s started for %s", config.agent_id, config.project_id)
        return ""

    async def deliver(self, agent_id: str, envelope: dict[str, Any]) -> None:
        """Run the scripted behaviour and queue whatever it reports back."""
        self.delivered.append((agent_id, envelope))
        config = self.configs.get(agent_id)
        if config is None or self.behaviour is None:
            return
        produced = self.behaviour(config, envelope)
        if hasattr(produced, "__await__"):
            produced = await produced  # type: ignore[assignment]
        for message in produced or []:
            self.emit(message, config=config)

    def emit(self, message: A2AMessage, *, config: ProjectAgentConfig | None = None) -> A2AMessage:
        """Queue a message from an agent to NEXUS SEED."""
        if config is not None:
            if message.source_agent_id is None:
                message.source_agent_id = config.agent_id
            if message.project_id is None:
                message.project_id = config.project_id
        self._outbox.append(message)
        return message

    async def poll(self) -> list[A2AMessage]:
        """Return and clear queued agent messages."""
        pending, self._outbox = self._outbox, []
        return pending

    async def stop(self, agent_id: str) -> None:
        """Forget the agent."""
        self.stopped.append(agent_id)
        self.configs.pop(agent_id, None)

    async def health(self, agent_id: str) -> bool:
        """Whether the agent is still registered."""
        return agent_id in self.configs


class A2AAgentRuntime:
    """Seam for a real external Agent Runtime reached over A2A.

    Deliberately thin and unfinished in this phase: it records what would be
    sent so the orchestration can be wired and reviewed, and leaves the actual
    transport to the existing ``providers/a2a.py`` boundary.  Nothing in the
    orchestrator changes when this replaces :class:`InProcessAgentRuntime` —
    that is the point of the interface.
    """

    name = "a2a"

    def __init__(self, *, endpoint: str, transport: Any | None = None) -> None:
        self.endpoint = endpoint
        self.transport = transport
        self._outbox: list[A2AMessage] = []

    async def spawn(self, config: ProjectAgentConfig) -> str:
        """Announce a new project to the external Agent Runtime."""
        if self.transport is None:
            raise NotImplementedError(
                "A2AAgentRuntime needs a transport; use InProcessAgentRuntime for tests"
            )
        await self.transport.spawn(config)  # pragma: no cover - needs a live endpoint
        return self.endpoint

    async def deliver(self, agent_id: str, envelope: dict[str, Any]) -> None:  # pragma: no cover
        """Send a goal or task to the remote agent."""
        if self.transport is None:
            raise NotImplementedError("A2AAgentRuntime needs a transport")
        await self.transport.deliver(agent_id, envelope)

    async def poll(self) -> list[A2AMessage]:  # pragma: no cover - needs a live endpoint
        """Return messages the remote agents have posted back."""
        if self.transport is None:
            return []
        received = await self.transport.poll()
        return list(received)

    async def stop(self, agent_id: str) -> None:  # pragma: no cover
        """Ask the remote runtime to stop the agent."""
        if self.transport is not None:
            await self.transport.stop(agent_id)

    async def health(self, agent_id: str) -> bool:  # pragma: no cover
        """Ask the remote runtime whether the agent is alive."""
        if self.transport is None:
            return False
        return bool(await self.transport.health(agent_id))


def status_message(project_id: str, summary: str, **payload: Any) -> A2AMessage:
    """Build a ``PROJECT_STATUS`` message (test/demo helper)."""
    return A2AMessage(
        type=A2AMessageType.PROJECT_STATUS,
        project_id=project_id,
        payload={"summary": summary, **payload},
    )


def completed_message(project_id: str, summary: str = "", **payload: Any) -> A2AMessage:
    """Build a ``PROJECT_COMPLETED`` message (test/demo helper)."""
    return A2AMessage(
        type=A2AMessageType.PROJECT_COMPLETED,
        project_id=project_id,
        payload={"summary": summary, **payload},
    )


def escalation(project_id: str, type: A2AMessageType, **payload: Any) -> A2AMessage:
    """Build an escalation message of ``type`` (test/demo helper)."""
    return A2AMessage(type=type, project_id=project_id, payload=dict(payload))


__all__ = [
    "A2AAgentRuntime",
    "AgentRuntime",
    "Behaviour",
    "InProcessAgentRuntime",
    "completed_message",
    "escalation",
    "status_message",
]
