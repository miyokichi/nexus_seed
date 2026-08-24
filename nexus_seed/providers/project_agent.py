"""A2A transport for a Project Agent — the wire side of one whole delegation.

:mod:`nexus_seed.providers.a2a` supplies the shared JSON-RPC client. This module
delegates one whole Project: NEXUS SEED hands over the
goal, its context, its constraints and a workspace, and the remote agent breaks
the goal down, chooses from its own skills, runs its own tools and answers with
the small set of messages the orchestrator understands.

What Skills that agent has is the agent's own configuration.  NEXUS SEED does
not read them, does not send them, and does not know what they are — its own
Skill roots describe what an Agent can do; Project Agent configuration itself
is owned by :mod:`nexus_seed.orchestrator_config`.

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

from typing import Any

from ..orchestrator.agent_runtime import AgentUnavailable, Dispatch, RemoteWorkLost
from ..orchestrator.models import A2AMessage, A2AMessageType, ProjectAgentConfig
from .a2a import (
    TERMINAL_STATES,
    UNSUPPORTED_STATES,
    A2AClient,
    A2AEndpoint,
    A2AProtocolError,
    task_state,
)

logger = logging.getLogger(__name__)

#: The wire type of the message that hands a whole Project to an Agent.
PROJECT_ASSIGNMENT = "PROJECT_ASSIGNMENT"

#: A2A extension URI for the provisioned-workspace contract.
#:
#: The spec reserves ``metadata`` for extension data keyed by an extension URI,
#: which is exactly the shape this needs: an agent that does not implement the
#: extension sees an ordinary message and keeps working, because the resources
#: it describes are already *in* the workspace.  That is what makes the whole
#: thing usable with Hermes, OpenCode or anything else, unmodified.
WORKSPACE_EXTENSION = "https://nexus-seed.dev/a2a/ext/provisioned-workspace/v1"

#: What the Project Agent is: an owner of one Goal, not a single tool call.
#:
#: NEXUS SEED deliberately does not say *how* to reach the goal here — no task
#: list, no skill order, no tool names.  Deciding that is the whole reason a
#: Project Agent exists.
PROJECT_AGENT_INSTRUCTION = """\
You are the Project Agent for one project of the NEXUS SEED orchestrator.

The whole project is yours. Work out the tasks the goal needs, in what order,
which of your own skills (if any) apply, and which tools to run. Check your own
results and keep going until the goal is actually met. NEXUS SEED will not
break the goal down for you, and does not know what skills you have.

The project assignment is in the context as `assignment`:
  goal              what has to be true when you are done
  context           what is already known about the project
  constraints       limits you must respect
  workspace         the directory to read and write in
  resources         what was granted to you, inside that workspace
  task              (only on a follow-up) an extra task for the same goal

Your workspace already holds everything you were granted. `resources` lists
each one as a workspace-relative `path` with an `access` of `read` or
`read_write`; `RESOURCES.md` in the workspace says the same thing. A `read`
file is a copy — editing it changes nothing anywhere. A `read_write` file is
carried back to where it came from when your work is accepted.

Work inside the workspace. Files elsewhere on this machine were not granted to
you, and reading around for them is not how to get them: if you need something
you do not have, ask with NEED_RESOURCE, naming what you need and why. NEXUS
SEED decides, and if it agrees the same task continues with the resource
provisioned into your workspace.

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

Stopping is answered the same way as finishing. Explaining why you stopped in a
sentence outside the object loses the explanation entirely, so put it in the
payload. A refusal looks exactly like this and nothing else:

{"messages": [{"type": "NEED_RESOURCE", "payload": {"required_resource": "sap_prior_year_sales", "reason": "The workspace only holds 2026-05..07; nothing here is SAP data and I will not fabricate a prior-year comparison."}}]}

Your last message is that object. Nothing before it, nothing after it.
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
        clock=time.monotonic,
    ) -> None:
        self.endpoint = endpoint
        self.client = client or A2AClient(endpoint)
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

    async def start(
        self, config: ProjectAgentConfig, envelope: dict[str, Any]
    ) -> Dispatch:
        """Hand the project (or an added task) over and come straight back.

        A Project takes as long as it takes, so this waits only for the Agent
        to *accept* the work.  The remote task id comes back as the dispatch
        handle; NEXUS SEED stores it and asks about it later, which is what
        lets a Project outlive the process that started it.
        """
        assignment = self.assignment(config, envelope)
        params = _send_params(assignment, config)
        try:
            result = await asyncio.to_thread(self.client.call, "message/send", params)
        except A2AProtocolError as exc:
            raise AgentUnavailable(f"project agent unreachable: {exc}") from exc

        if str(result.get("kind") or "") == "message":
            # Answered without ever becoming a task; nothing to ask about later.
            return Dispatch(messages=_messages(result.get("parts") or [], config))

        handle = str(result.get("id") or "")
        if task_state(result) in TERMINAL_STATES:
            return Dispatch(messages=self._finished(result, config))
        if not handle:
            raise AgentUnavailable("project agent returned a task without an id")
        logger.info(
            "project %s handed to agent %s as remote task %s",
            config.project_id,
            config.agent_id,
            handle,
        )
        return Dispatch(handle=handle)

    async def collect(
        self, config: ProjectAgentConfig, handle: str
    ) -> list[A2AMessage] | None:
        """Ask once whether ``handle`` has finished; ``None`` while it has not.

        A single question, never a wait, and nothing about the dispatch is kept
        here: the handle and the config both come from the caller, so an answer
        is still readable by a NEXUS SEED that restarted while the Agent worked.
        """
        try:
            task = await asyncio.to_thread(self.client.call, "tasks/get", {"id": handle})
        except A2AProtocolError as exc:
            if exc.task_not_found:
                raise RemoteWorkLost(
                    f"project agent no longer knows task {handle}"
                ) from exc
            raise AgentUnavailable(f"project agent unreachable: {exc}") from exc

        state = task_state(task)
        if state in UNSUPPORTED_STATES:
            await self.abandon(handle)
            raise AgentUnavailable(
                f"project agent task needs {state}, which this channel cannot supply"
            )
        if state not in TERMINAL_STATES:
            return None
        return self._finished(task, config)

    def _finished(self, task: dict[str, Any], config: ProjectAgentConfig) -> list[A2AMessage]:
        """Read a terminal task, or say why it is not an answer."""
        state = task_state(task)
        if state != "completed":
            raise AgentUnavailable(
                f"project agent task {state}: {_error_text(task) or 'no reason given'}"
            )
        return _messages(_result_parts(task), config)


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
            "nexus_seed_endpoint": config.nexus_seed_a2a_endpoint,
            # What was provisioned into the workspace, as workspace-relative
            # paths.  An Agent that ignores this still finds the same files by
            # reading its workspace, which is the point: the manifest describes
            # the directory, it does not replace it.
            "resources": list(config.resources),
        }
        if envelope.get("kind") == "ADD_TASK":
            assignment["task"] = envelope.get("task") or {}
        return assignment

    async def abandon(self, handle: str) -> bool:
        """Stop caring about a dispatch, telling the Agent if it will listen.

        Best effort by contract: NEXUS SEED owns its own lifecycle, so an Agent
        that refuses, times out or has forgotten the task is logged and
        otherwise ignored.
        """
        if not handle:
            return False
        try:
            await asyncio.to_thread(self.client.call, "tasks/cancel", {"id": handle})
            return True
        except Exception:  # noqa: BLE001 - a refused cancel must not break us
            logger.warning("tasks/cancel failed for task %s", handle, exc_info=True)
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
            # A2A extension data belongs in `metadata` under a URI-namespaced
            # key, so an agent that does not know this extension ignores it and
            # the request stays a plain, valid A2A message.  Nothing here is
            # load-bearing: the same facts are in the workspace on disk.
            f"{WORKSPACE_EXTENSION}/workspace": config.workspace,
            f"{WORKSPACE_EXTENSION}/resources": list(config.resources),
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
    "WORKSPACE_EXTENSION",
]
