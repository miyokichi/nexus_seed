"""AgentRuntime — where a Project Agent actually runs.

NEXUS SEED does not contain an agent loop.  It starts a Project Agent somewhere
(in this process, as a subprocess, or as a remote A2A endpoint), hands it a
:class:`ProjectAgentConfig`, and afterwards talks to it only over A2A.

Two implementations ship:

* :class:`InProcessAgentRuntime` — a scripted, network-free runtime used by the
  tests, exactly as ``FakeLLMBackend`` is used for the LLM boundary.
* :class:`A2AAgentRuntime` — a real external Agent Runtime (Little Agent or
  anything else that answers A2A), reached through a transport that reuses the
  existing ``providers/a2a.py`` boundary rather than a second protocol client.
  Nothing in the orchestrator changes when one replaces the other; that is the
  point of the interface.

Handing a Project over and getting the answer are two steps, because a Project
takes as long as it takes.  :meth:`deliver` returns a :class:`Dispatch` — what
the Agent already answered, plus a ``handle`` to ask about later — and
:meth:`collect` asks whether that handle has finished.  The orchestrator stores
the handle, so a Project that is being worked on survives a restart of NEXUS
SEED.

Messages an Agent pushes are *queued*, not delivered re-entrantly: the
orchestrator drains them after the current step, the same way the Runtime drains
emitted events.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .models import A2AMessage, A2AMessageType, ProjectAgentConfig

logger = logging.getLogger("nexus_seed.orchestrator.agent_runtime")


class AgentUnavailable(RuntimeError):
    """The Project Agent could not be reached, or did not answer in contract.

    Deliberately not an escalation: an Agent that cannot be talked to is a
    transport problem and is retryable, while ``NEED_CAPABILITY`` and friends
    are statements *by* a working Agent about its Project.  Confusing the two
    would block a Project for a restart of the agent process.
    """


class RemoteWorkLost(AgentUnavailable):
    """The Agent no longer knows the work NEXUS SEED handed it.

    Not the same as being unreachable: the Agent answered, and its answer was
    that the work is gone.  NEXUS SEED holds the durable Project, so this is
    recoverable — the assignment can be handed over again, and *only* on this
    answer, since anything vaguer could run the same work twice.
    """


@dataclass
class Dispatch:
    """The result of handing work to an Agent.

    ``messages`` is what the Agent said straight away; ``handle`` is what to ask
    about later.  An empty handle means there is nothing outstanding — either
    the Agent answered at once, or it pushes its answers instead.
    """

    handle: str = ""
    messages: list[A2AMessage] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        """Whether an answer is still owed under :attr:`handle`."""
        return bool(self.handle)


@runtime_checkable
class AgentRuntime(Protocol):
    """Somewhere a generic Project Agent can be started and talked to."""

    name: str

    async def spawn(self, config: ProjectAgentConfig) -> str:
        """Start an agent for ``config`` and return its endpoint (or ``""``)."""
        ...

    async def attach(self, config: ProjectAgentConfig) -> None:
        """Re-adopt an Agent that already exists, so it can be talked to again.

        Called for an Agent read back from the database — after a restart the
        runtime has never seen it, but the Project record says it owns the
        Project, so the config is rebuilt and handed back rather than a second
        Agent being started for the same Project.
        """
        ...

    async def deliver(self, agent_id: str, envelope: dict[str, Any]) -> Dispatch:
        """Hand the agent a goal or an added task, without waiting it out."""
        ...

    async def collect(self, agent_id: str, handle: str) -> list[A2AMessage] | None:
        """Return the answer for ``handle``, or ``None`` while it is still working."""
        ...

    async def abandon(self, agent_id: str, handle: str) -> None:
        """Give up on one dispatch, without giving up on the Agent itself."""
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

    async def attach(self, config: ProjectAgentConfig) -> None:
        """Re-adopt an agent that was recorded by an earlier run."""
        self.configs[config.agent_id] = config

    async def deliver(self, agent_id: str, envelope: dict[str, Any]) -> Dispatch:
        """Run the scripted behaviour and queue whatever it reports back.

        Nothing is outstanding afterwards: a scripted agent answers at once, so
        the dispatch carries no handle and the messages are drained as usual.
        """
        self.delivered.append((agent_id, envelope))
        config = self.configs.get(agent_id)
        if config is None or self.behaviour is None:
            return Dispatch()
        produced = self.behaviour(config, envelope)
        if hasattr(produced, "__await__"):
            produced = await produced  # type: ignore[assignment]
        for message in produced or []:
            self.emit(message, config=config)
        return Dispatch()

    async def collect(self, agent_id: str, handle: str) -> list[A2AMessage] | None:
        """Nothing is ever outstanding here; answers arrive through ``poll``."""
        return []

    async def abandon(self, agent_id: str, handle: str) -> None:
        """Nothing is ever outstanding here, so there is nothing to give up on."""
        return None

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


@runtime_checkable
class ProjectAgentTransport(Protocol):
    """How :class:`A2AAgentRuntime` reaches a real Project Agent.

    Everything about HTTP, JSON-RPC and A2A framing lives behind this — see
    :mod:`nexus_seed.providers.project_agent`.  The orchestrator only knows
    that a Project can be handed over and that messages come back.
    """

    async def open(self, config: ProjectAgentConfig) -> str:
        """Make the Agent ready and return the endpoint it is reached at."""
        ...

    async def start(
        self, config: ProjectAgentConfig, envelope: dict[str, Any]
    ) -> Dispatch:
        """Hand over a goal or an added task and return without waiting it out."""
        ...

    async def collect(
        self, config: ProjectAgentConfig, handle: str
    ) -> list[A2AMessage] | None:
        """Return the answer for ``handle``, or ``None`` while it is still working."""
        ...

    async def abandon(self, handle: str) -> bool:
        """Stop caring about a dispatch, telling the Agent if it will listen."""
        ...

    async def close(self, agent_id: str) -> None:
        """Release whatever the Agent was holding."""
        ...

    async def alive(self) -> bool:
        """Whether the remote Agent Runtime is answering."""
        ...


class A2AAgentRuntime:
    """A Project Agent that runs in a separate process, reached over A2A.

    The remote runtime is generic and holds no project state: the Project *is*
    the delegation, so this runtime keeps only the config each Agent was given
    (rebuilt from the database through ``attach`` after a restart) and queues
    what the Agent answers for the orchestrator to drain.

    A transport failure raises :class:`AgentUnavailable` rather than being
    turned into an escalation, so an agent process that is simply down never
    looks like a Project that lacks a Capability.
    """

    name = "a2a"

    def __init__(self, transport: ProjectAgentTransport) -> None:
        self.transport = transport
        self.configs: dict[str, ProjectAgentConfig] = {}
        self._outbox: list[A2AMessage] = []

    async def spawn(self, config: ProjectAgentConfig) -> str:
        """Make the remote runtime ready for this project and record its config."""
        endpoint = await self.transport.open(config)
        self.configs[config.agent_id] = config
        logger.info(
            "project %s delegated to external agent %s at %s",
            config.project_id,
            config.agent_id,
            endpoint or "(no endpoint)",
        )
        return endpoint

    async def attach(self, config: ProjectAgentConfig) -> None:
        """Re-adopt an Agent recorded by an earlier run, without a remote call."""
        self.configs[config.agent_id] = config

    async def deliver(self, agent_id: str, envelope: dict[str, Any]) -> Dispatch:
        """Hand the whole Project (or one added Task) to the remote Agent.

        This returns as soon as the Agent has taken the work, not when it has
        finished it: a Project runs for as long as it needs, and NEXUS SEED
        stores the returned handle rather than holding a process open.
        """
        config = self._config(agent_id)
        return await self.transport.start(config, envelope)

    async def collect(self, agent_id: str, handle: str) -> list[A2AMessage] | None:
        """Ask whether the Agent has finished the work under ``handle``."""
        return await self.transport.collect(self._config(agent_id), handle)

    async def abandon(self, agent_id: str, handle: str) -> None:
        """Give up on one dispatch without giving up on the Agent."""
        await self.transport.abandon(handle)

    def _config(self, agent_id: str) -> ProjectAgentConfig:
        """Return what this Agent was started with, or say it is unknown."""
        config = self.configs.get(agent_id)
        if config is None:
            raise AgentUnavailable(
                f"agent {agent_id} is not known to this runtime; it must be "
                "spawned or attached before work is delegated to it"
            )
        return config

    async def poll(self) -> list[A2AMessage]:
        """Return and clear what the remote Agents have reported."""
        pending, self._outbox = self._outbox, []
        return pending

    async def stop(self, agent_id: str) -> None:
        """Release the remote Agent and forget its config."""
        self.configs.pop(agent_id, None)
        await self.transport.close(agent_id)

    async def health(self, agent_id: str) -> bool:
        """Whether this Agent is known here and its runtime is answering."""
        if agent_id not in self.configs:
            return False
        return bool(await self.transport.alive())


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
    "AgentUnavailable",
    "Behaviour",
    "Dispatch",
    "InProcessAgentRuntime",
    "ProjectAgentTransport",
    "RemoteWorkLost",
    "completed_message",
    "escalation",
    "status_message",
]
