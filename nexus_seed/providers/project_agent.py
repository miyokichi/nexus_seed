"""A2A transport for a Project Agent — the wire side of one whole delegation.

:mod:`nexus_seed.providers.a2a` delegates *one Skill execution* to a remote
agent.  This module delegates *one whole Project*: NEXUS SEED hands over the
goal, its context, its constraints, a workspace and the Skill contracts it may
choose between, and the remote agent breaks the goal down, picks its own
skills, runs its own tools and answers with the small set of messages the
orchestrator understands.

It reuses the existing :class:`~nexus_seed.providers.a2a.A2AClient`, endpoint
settings and poll loop rather than adding a second protocol client, and it is
the only place that knows what a Project Assignment looks like on the wire —
``orchestrator/`` never sees HTTP or A2A framing.

    NEXUS SEED  ->  PROJECT_ASSIGNMENT  ->  Project Agent
    Project Agent  ->  PROJECT_STATUS / PROJECT_COMPLETED / NEED_* ->  NEXUS SEED

A remote agent that cannot be reached, times out, fails its task or answers
outside the contract raises :class:`~nexus_seed.orchestrator.agent_runtime.
AgentUnavailable`.  That is deliberately a *different thing* from an Agent
reporting that it lacks a Capability: the first is a transport problem and is
retryable, the second is a real blocker on the Project.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ..orchestrator.agent_runtime import AgentUnavailable
from ..orchestrator.models import A2AMessage, A2AMessageType, ProjectAgentConfig
from .a2a import (
    A2AClient,
    A2AEndpoint,
    A2AProtocolError,
    A2ATaskUnfinished,
    await_task,
    task_state,
)
from .skills import SkillCatalog

logger = logging.getLogger(__name__)

#: The wire type of the message that hands a whole Project to an Agent.
PROJECT_ASSIGNMENT = "PROJECT_ASSIGNMENT"

#: What the Project Agent is: an owner of one Goal, not a single tool call.
#:
#: NEXUS SEED deliberately does not say *how* to reach the goal here — no task
#: list, no skill order, no tool names.  Deciding that is the whole reason a
#: Project Agent exists.
PROJECT_AGENT_INSTRUCTION = """\
You are the Project Agent for one project of the NEXUS SEED orchestrator.

The whole project is yours. Work out the tasks the goal needs, in what order,
which of the available skills (if any) apply, and which tools to run. Check
your own results and keep going until the goal is actually met. NEXUS SEED
will not break the goal down for you and will not tell you which skill to use.

The project assignment is in the context as `assignment`:
  goal              what has to be true when you are done
  context           what is already known about the project
  constraints       limits you must respect
  workspace         the directory to read and write in
  available_skills  reusable procedures you may follow, with their contracts
  task              (only on a follow-up) an extra task for the same goal

Answer with one JSON object: {"messages": [{"type": ..., "payload": {...}}]}.

Your final message must be that object and nothing else: no sentence before it,
no explanation after it, no code fence. Whatever you want to say — what you
found, what you could not do, why you stopped — goes inside a payload, because
that is the part NEXUS SEED records. Anything outside the object is lost.

Use only these message types, and no others:

  PROJECT_STATUS          progress worth recording. payload: {"summary": "..."}
  PROJECT_COMPLETED       the goal is met. payload: {"summary": "what you found or did"}
  NEED_CAPABILITY         you lack an ability. payload: {"required_capability": "...", "reason": "..."}
  NEED_RESOURCE           you lack data or a file. payload: {"required_resource": "...", "reason": "..."}
  NEED_PERMISSION         you lack an authorisation. payload: {"required_permission": "...", "reason": "..."}
  PROJECT_BLOCKED         blocked for some other reason. payload: {"reason": "..."}
  NEED_HUMAN_INPUT        only a person can answer. payload: {"question": "...", "reason": "..."}
  DISCOVERED_NEW_PROJECT  you found a separate problem. payload: {"request": "...", "reason": "..."}

Rules:
- End with exactly one of PROJECT_COMPLETED, NEED_CAPABILITY, NEED_RESOURCE,
  NEED_PERMISSION, PROJECT_BLOCKED or NEED_HUMAN_INPUT. Send PROJECT_STATUS
  before it as often as is useful.
- If something you cannot supply is missing, escalate once and stop. Do not
  retry forever and do not substitute a different input to keep going.
- Never claim PROJECT_COMPLETED for work you did not verify.
- You cannot create projects. Report a separate problem with
  DISCOVERED_NEW_PROJECT and let NEXUS SEED decide what happens to it.
"""

#: The only shape an answer may take.  Typed output is required, never guessed.
REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["messages"],
    "properties": {
        "messages": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["type", "payload"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [message.value for message in A2AMessageType],
                    },
                    "payload": {"type": "object"},
                },
            },
        }
    },
}


def skill_contracts(
    catalog: SkillCatalog, names: Sequence[str] = ()
) -> dict[str, dict[str, Any]]:
    """Return the contract of each named Skill, for an Agent to choose between.

    The contract is what a Skill *is* — what it takes, what it produces and how
    to think about it.  NEXUS SEED never orders these or decides which one
    applies; it only says which ones exist.
    """
    wanted = set(names)
    contracts: dict[str, dict[str, Any]] = {}
    for skill in catalog.list():
        if wanted and skill.name not in wanted:
            continue
        descriptor = skill.descriptor
        contracts[skill.name] = {
            "name": descriptor.name,
            "description": descriptor.description,
            "capabilities": list(skill.capability_names),
            "input_ports": list(descriptor.input_ports),
            "output_ports": list(descriptor.output_ports),
            "output_schema": descriptor.output_schema,
            "instructions": descriptor.instructions,
        }
    return contracts


class A2AProjectAgentTransport:
    """Sends a Project Assignment over A2A and reads the Agent's answer back.

    One remote endpoint serves every Project Agent: the agent is generic and a
    project is what one delegation carries, so there is no per-project process
    to start or stop here.
    """

    def __init__(
        self,
        endpoint: A2AEndpoint,
        *,
        client: A2AClient | None = None,
        skills: Mapping[str, dict[str, Any]] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.endpoint = endpoint
        self.client = client or A2AClient(endpoint)
        self.skills = dict(skills or {})
        self._clock = clock

    async def open(self, config: ProjectAgentConfig) -> str:
        """Check the remote runtime answers as an A2A agent and return its URL.

        A missing or unreadable Agent Card means the runtime is unavailable.  It
        never means the Project lacks a Capability.
        """
        try:
            card = await asyncio.to_thread(self.client.agent_card)
        except A2AProtocolError as exc:
            raise AgentUnavailable(str(exc)) from exc
        logger.info(
            "project %s will run on remote agent %s (%s)",
            config.project_id,
            card.name or "unnamed",
            self.endpoint.url,
        )
        return self.endpoint.url

    async def send(
        self, config: ProjectAgentConfig, envelope: dict[str, Any]
    ) -> list[A2AMessage]:
        """Delegate the project (or an added task) and return what came back."""
        assignment = self.assignment(config, envelope)
        params = _send_params(assignment, config)
        try:
            result = await asyncio.to_thread(self.client.call, "message/send", params)
        except A2AProtocolError as exc:
            raise AgentUnavailable(f"project agent unreachable: {exc}") from exc

        if str(result.get("kind") or "") == "message":
            parts = result.get("parts") or []
        else:
            try:
                task = await await_task(
                    self.client,
                    self.endpoint,
                    result,
                    clock=self._clock,
                    cancel=self.cancel,
                )
            except A2ATaskUnfinished as exc:
                raise AgentUnavailable(f"project agent did not answer: {exc}") from exc
            state = task_state(task)
            if state != "completed":
                raise AgentUnavailable(
                    f"project agent task {state}: {_error_text(task) or 'no reason given'}"
                )
            parts = _result_parts(task)
        return _messages(parts, config)

    def assignment(
        self, config: ProjectAgentConfig, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the PROJECT_ASSIGNMENT wire object for one delegation.

        Everything here is derived from :class:`ProjectAgentConfig` and the
        envelope the gateway already sends; nothing about a project is tracked
        a second time on this side.
        """
        assignment: dict[str, Any] = {
            "type": PROJECT_ASSIGNMENT,
            "project_id": config.project_id,
            "agent_id": config.agent_id,
            "goal": config.goal,
            "context": dict(config.project_context),
            "constraints": dict(config.constraints),
            "workspace": config.workspace,
            "available_skills": [
                self.skills.get(name, {"name": name}) for name in config.available_skills
            ],
            "nexus_seed_endpoint": config.nexus_seed_a2a_endpoint,
        }
        if envelope.get("kind") == "ADD_TASK":
            assignment["task"] = envelope.get("task") or {}
        return assignment

    async def cancel(self, task_id: str | None) -> bool:
        """Best-effort remote cancel; our own lifecycle never depends on it."""
        if not task_id:
            return False
        try:
            await asyncio.to_thread(self.client.call, "tasks/cancel", {"id": task_id})
            return True
        except Exception:  # noqa: BLE001 - a refused cancel must not break us
            logger.warning("tasks/cancel failed for task %s", task_id, exc_info=True)
            return False

    async def close(self, agent_id: str) -> None:
        """Nothing to shut down: one delegation is one remote task."""
        return None

    async def alive(self) -> bool:
        """Whether the remote Agent Runtime is answering right now."""
        try:
            await asyncio.to_thread(self.client.agent_card, refresh=True)
        except A2AProtocolError:
            return False
        return True


# --- wire construction and reading ---------------------------------------


def _send_params(assignment: dict[str, Any], config: ProjectAgentConfig) -> dict[str, Any]:
    """Wrap a Project Assignment in one A2A ``message/send`` request."""
    return {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": f"{config.project_id}:{config.agent_id}",
            "parts": [
                {
                    "kind": "data",
                    "data": {
                        "instruction": PROJECT_AGENT_INSTRUCTION,
                        "context": {"assignment": assignment},
                        "output_schema": REPLY_SCHEMA,
                    },
                }
            ],
        },
        "configuration": {
            "blocking": True,
            "acceptedOutputModes": ["application/json"],
        },
        "metadata": {
            "nexus_seed/project_id": config.project_id,
            "nexus_seed/agent_id": config.agent_id,
        },
    }


def _result_parts(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect artifact parts, falling back to the final status message."""
    parts: list[dict[str, Any]] = []
    for artifact in task.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        parts.extend(part for part in artifact.get("parts") or [] if isinstance(part, dict))
    if parts:
        return parts
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    message = status.get("message") if isinstance(status, dict) else None
    if isinstance(message, dict):
        return [part for part in message.get("parts") or [] if isinstance(part, dict)]
    return []


def _error_text(task: dict[str, Any]) -> str:
    """The failure reason a remote agent put in its final status message."""
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    message = status.get("message") if isinstance(status, dict) else None
    if not isinstance(message, dict):
        return ""
    texts = [
        str(part.get("text") or "")
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("kind") == "text"
    ]
    return " ".join(text for text in texts if text).strip()


def _messages(parts: list[dict[str, Any]], config: ProjectAgentConfig) -> list[A2AMessage]:
    """Read the Agent's answer into orchestrator messages.

    Structure is required, not repaired: an answer that carries no message the
    orchestrator understands is a failed delegation, because acting on a guess
    about what an Agent meant is worse than reporting that it did not answer.
    """
    messages: list[A2AMessage] = []
    for entry in _reported(parts):
        try:
            message_type = A2AMessageType(entry.get("type"))
        except ValueError:
            logger.warning(
                "project %s: agent sent unknown message type %r",
                config.project_id,
                entry.get("type"),
            )
            continue
        payload = entry.get("payload")
        messages.append(
            A2AMessage(
                type=message_type,
                project_id=config.project_id,
                source_agent_id=config.agent_id,
                payload=dict(payload) if isinstance(payload, dict) else {},
            )
        )
    if not messages:
        raise AgentUnavailable(
            "project agent answered without any message NEXUS SEED understands"
        )
    return messages


def _reported(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the raw ``{type, payload}`` entries found in the answer's parts."""
    entries: list[dict[str, Any]] = []
    for part in parts:
        if part.get("kind") != "data" or not isinstance(part.get("data"), dict):
            continue
        data = part["data"]
        reported = data.get("messages")
        if isinstance(reported, list):
            entries.extend(item for item in reported if isinstance(item, dict))
        elif data.get("type"):
            # One message sent bare, which is the shape the contract describes
            # for a single answer; still explicit, so still accepted.
            entries.append(data)
    return entries


__all__ = [
    "A2AProjectAgentTransport",
    "PROJECT_AGENT_INSTRUCTION",
    "PROJECT_ASSIGNMENT",
    "REPLY_SCHEMA",
    "skill_contracts",
]
