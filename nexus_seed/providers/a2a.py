"""A2A execution provider — delegate cognitive work to an external Agent Runtime.

NEXUS SEED contains no LLM agent loop.  When a Skill must actually be *thought
through*, the ProcessDefinition is bound to a provider whose adapter speaks the
A2A protocol over HTTP, and some other process entirely (Little Agent, Hermes,
anything else that answers A2A) does the LLM and Tool work.

The boundary is deliberately narrow.  This module converts one
:class:`DelegationRequest` into an A2A ``message/send``, polls ``tasks/get``
until the task reaches a terminal state, and converts the artifacts back into a
:class:`DelegationResult`.  It selects no skills, runs no tools, keeps no task
database and imports nothing from any particular agent product::

    Durability = NEXUS SEED        Execution = remote Agent

Only ``blocking + polling`` is implemented; SSE and push notifications are
deliberately out of scope.  Remote task state is treated as volatile — if the
remote store is lost across a restart, the existing Continuation and retry
policy re-runs the delegation rather than this adapter reconstructing it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .adapters import ProviderUnavailableBeforeStart
from .models import (
    DelegationRequest,
    DelegationResult,
    DelegationStatus,
    ExecutionProvider,
    ProviderHealth,
    ProviderKind,
    ProviderStatus,
)

logger = logging.getLogger(__name__)

#: Where an A2A agent publishes its Agent Card.
AGENT_CARD_PATH = "/.well-known/agent-card.json"

#: A2A task states that end the task.  Nothing else stops the poll loop.
TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})

#: States that ask for something a blocking delegation cannot supply.
UNSUPPORTED_STATES = frozenset({"input-required", "auth-required"})


class A2AProtocolError(RuntimeError):
    """The remote endpoint did not answer with a usable A2A response."""


@dataclass(frozen=True)
class A2AAgentCard:
    """The remote agent's self-description, used for diagnostics only.

    Advertised skills are *not* imported as NEXUS SEED Skills and never
    establish a Capability claim — what the remote runtime can do is its own
    business (Invariant 125).
    """

    name: str = ""
    description: str = ""
    version: str = ""
    url: str = ""
    transport: str = ""
    skills: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> A2AAgentCard:
        """Read the fields we care about; ignore the rest of the card."""
        if not isinstance(data, dict):
            raise A2AProtocolError("agent card must be a JSON object")
        skills = []
        for skill in data.get("skills") or []:
            if isinstance(skill, dict) and skill.get("id"):
                skills.append(str(skill["id"]))
            elif isinstance(skill, dict) and skill.get("name"):
                skills.append(str(skill["name"]))
            elif isinstance(skill, str):
                skills.append(skill)
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            version=str(data.get("version") or ""),
            url=str(data.get("url") or ""),
            transport=str(data.get("preferredTransport") or data.get("transport") or ""),
            skills=tuple(skills),
            raw=data,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "url": self.url,
            "transport": self.transport,
            "skills": list(self.skills),
        }


@dataclass(frozen=True)
class A2AEndpoint:
    """Connection settings for one remote A2A agent.

    The token is read from the environment by name so no secret is ever stored
    in a provider record, a binding or the database.
    """

    url: str
    token_env: str | None = None
    poll_interval_seconds: float = 1.0
    timeout_seconds: float = 300.0
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"A2A url must be an http(s) URL: {self.url!r}")
        for name in ("poll_interval_seconds", "timeout_seconds", "request_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @property
    def token(self) -> str | None:
        """Return the bearer token from the environment, if one is configured."""
        if not self.token_env:
            return None
        return os.environ.get(self.token_env, "").strip() or None


class A2AClient:
    """A small blocking JSON-RPC client for the A2A HTTP transport.

    Blocking on purpose: it uses only the standard library, and the adapter
    keeps the event loop free by running each call in a worker thread, the same
    way :mod:`nexus_seed.backends.llm` calls its provider.
    """

    def __init__(self, endpoint: A2AEndpoint) -> None:
        self.endpoint = endpoint
        self._card: A2AAgentCard | None = None

    def agent_card(self, *, refresh: bool = False) -> A2AAgentCard:
        """Fetch and cache the Agent Card.

        Raises :class:`A2AProtocolError` when the endpoint does not answer as
        an A2A agent.  A missing card is a *provider* problem, never a missing
        Capability.
        """
        if self._card is not None and not refresh:
            return self._card
        url = urljoin(self.endpoint.url.rstrip("/") + "/", AGENT_CARD_PATH.lstrip("/"))
        request = Request(url, method="GET", headers=self._headers())
        try:
            with urlopen(request, timeout=self.endpoint.request_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise A2AProtocolError(f"agent card unavailable at {url}: {exc}") from exc
        self._card = A2AAgentCard.from_dict(payload)
        return self._card

    def call(self, method: str, params: dict) -> dict:
        """Perform one JSON-RPC call and return its ``result`` object."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        ).encode("utf-8")
        request = Request(
            self.endpoint.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", **self._headers()},
        )
        try:
            with urlopen(request, timeout=self.endpoint.request_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise A2AProtocolError(f"{method} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise A2AProtocolError(f"{method} returned a non-object response")
        if payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise A2AProtocolError(f"{method} rejected: {message}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise A2AProtocolError(f"{method} returned no result object")
        return result

    def _headers(self) -> dict:
        token = self.endpoint.token
        return {"Authorization": f"Bearer {token}"} if token else {}


class A2ATaskUnfinished(RuntimeError):
    """A remote task that had already started did not reach ``completed``.

    Execution began, so this is a failed attempt rather than a free failover to
    another provider — the caller decides what a failed attempt means for it.
    """

    def __init__(self, reason: str, *, state: str = "", task_id: str | None = None) -> None:
        super().__init__(reason)
        self.state = state
        self.task_id = task_id


def task_state(task: dict) -> str:
    """Return the lower-cased A2A ``TaskState`` of ``task``."""
    status = task.get("status")
    if isinstance(status, dict):
        return str(status.get("state") or "").lower()
    return str(status or "").lower()


async def await_task(
    client: A2AClient,
    endpoint: A2AEndpoint,
    task: dict,
    *,
    clock=time.monotonic,
    cancel: Callable[[str | None], Awaitable[bool]] | None = None,
) -> dict:
    """Poll ``tasks/get`` until ``task`` reaches a terminal state and return it.

    The one poll loop in this codebase, so every caller that sends an A2A
    message and waits for the answer obeys the same timeout, cancel and
    unsupported-state rules.  Raises :class:`A2ATaskUnfinished` when the task
    cannot get there.
    """
    task_id = str(task.get("id") or "") or None
    deadline = clock() + endpoint.timeout_seconds
    while task_state(task) not in TERMINAL_STATES:
        state = task_state(task)
        if state in UNSUPPORTED_STATES:
            if cancel is not None:
                await cancel(task_id)
            raise A2ATaskUnfinished(
                f"remote task needs {state}, which blocking delegation cannot supply",
                state=state,
                task_id=task_id,
            )
        if task_id is None:
            raise A2ATaskUnfinished("remote agent returned a task without an id")
        if clock() >= deadline:
            if cancel is not None:
                await cancel(task_id)
            raise A2ATaskUnfinished(
                f"remote task timed out after {endpoint.timeout_seconds}s",
                state=state,
                task_id=task_id,
            )
        await asyncio.sleep(endpoint.poll_interval_seconds)
        try:
            task = await asyncio.to_thread(client.call, "tasks/get", {"id": task_id})
        except A2AProtocolError as exc:
            raise A2ATaskUnfinished(str(exc), state=state, task_id=task_id) from exc
    return task


class A2AAgentAdapter:
    """Delegate one structured request to a remote A2A agent and come back.

    This adapter is generic: the same class serves any A2A-speaking runtime.
    Swapping one remote agent for another is a configuration change, never a
    Core change.
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

    async def agent_card(self, *, refresh: bool = False) -> A2AAgentCard:
        """Return the remote Agent Card without blocking the event loop."""
        return await asyncio.to_thread(self.client.agent_card, refresh=refresh)

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        """Send, poll to a terminal state, and convert the result back."""
        params = self._send_params(request)
        try:
            result = await asyncio.to_thread(self.client.call, "message/send", params)
        except A2AProtocolError as exc:
            # Nothing was started, so another provider may still be tried.
            raise ProviderUnavailableBeforeStart(str(exc)) from exc

        if str(result.get("kind") or "") == "message":
            return self._from_parts(
                request, result.get("parts") or [], state="completed", task_id=None
            )

        try:
            task = await await_task(
                self.client,
                self.endpoint,
                result,
                clock=self._clock,
                cancel=self.cancel,
            )
        except A2ATaskUnfinished as exc:
            # Execution had already started; this is a failed attempt, never a
            # free failover to a second provider.
            return self._failed(request, str(exc), task_id=exc.task_id, state=exc.state)

        task_id = str(task.get("id") or "") or None
        state = self._state(task)
        if state != "completed":
            message = self._error_text(task) or f"remote task {state}"
            return self._failed(request, message, task_id=task_id, state=state)
        return self._from_parts(
            request, self._result_parts(task), state=state, task_id=task_id
        )

    async def cancel(self, task_id: str | None) -> bool:
        """Ask the remote agent to cancel; never let this break our own cancel.

        Cancellation is best effort by contract: NEXUS SEED owns its own
        lifecycle, so a remote agent that refuses, times out or has forgotten
        the task is logged and otherwise ignored.
        """
        if not task_id:
            return False
        try:
            await asyncio.to_thread(self.client.call, "tasks/cancel", {"id": task_id})
            return True
        except Exception:
            logger.warning("A2A tasks/cancel failed for task %s", task_id, exc_info=True)
            return False

    # --- request construction ---------------------------------------------

    def _send_params(self, request: DelegationRequest) -> dict:
        definition = request.process_definition or {}
        data: dict[str, Any] = {
            "instruction": self._instruction(request),
            "context": {
                "typed_inputs": request.typed_inputs,
                "relevant_context": request.relevant_context,
                "constraints": request.constraints,
                "required_capabilities": request.required_capabilities,
            },
        }
        output_schema = definition.get("output_schema") or {}
        if output_schema:
            data["output_schema"] = output_schema
        elif definition.get("output_types"):
            data["output_types"] = list(definition["output_types"])
        return {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": str(request.invocation_id),
                "parts": [{"kind": "data", "data": data}],
            },
            "configuration": {"blocking": True, "acceptedOutputModes": ["application/json", "text/plain"]},
            "metadata": self._correlation(request),
        }

    @staticmethod
    def _instruction(request: DelegationRequest) -> str:
        """Compose the Skill's cognitive procedure with this Work's objective."""
        definition = request.process_definition or {}
        parts = [
            str(definition.get("instructions") or "").strip(),
            str(request.objective or "").strip(),
        ]
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _correlation(request: DelegationRequest) -> dict:
        """Namespaced ids for observability; a remote agent may ignore them."""
        metadata = {
            "nexus_seed/invocation_id": str(request.invocation_id),
            "nexus_seed/process_instance_id": str(request.process_instance_id),
            "nexus_seed/idempotency_key": request.idempotency_key,
        }
        for source_key, target in (
            ("work_requirement_id", "nexus_seed/work_id"),
            ("project_id", "nexus_seed/project_id"),
            ("goal_id", "nexus_seed/goal_id"),
            ("attempt", "nexus_seed/attempt"),
        ):
            value = (request.metadata or {}).get(source_key)
            if value is not None:
                metadata[target] = str(value)
        return metadata

    # --- result conversion -------------------------------------------------

    @staticmethod
    def _state(task: dict) -> str:
        return task_state(task)

    @staticmethod
    def _error_text(task: dict) -> str:
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        message = status.get("message") if isinstance(status, dict) else None
        if isinstance(message, dict):
            texts = [
                str(part.get("text") or "")
                for part in message.get("parts") or []
                if isinstance(part, dict) and part.get("kind") == "text"
            ]
            return " ".join(text for text in texts if text).strip()
        return ""

    @staticmethod
    def _result_parts(task: dict) -> list[dict]:
        """Collect artifact parts, falling back to the final status message."""
        parts: list[dict] = []
        for artifact in task.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            for part in artifact.get("parts") or []:
                if isinstance(part, dict):
                    parts.append(part)
        if parts:
            return parts
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        message = status.get("message") if isinstance(status, dict) else None
        if isinstance(message, dict):
            return [part for part in message.get("parts") or [] if isinstance(part, dict)]
        return []

    def _from_parts(
        self, request: DelegationRequest, parts: list, *, state: str, task_id: str | None
    ) -> DelegationResult:
        definition = request.process_definition or {}
        declared = [str(item) for item in definition.get("output_types") or []]
        wants_structure = bool(definition.get("output_schema"))
        data_parts = [
            part["data"]
            for part in parts
            if isinstance(part, dict)
            and part.get("kind") == "data"
            and isinstance(part.get("data"), dict)
        ]
        text_parts = [
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and part.get("kind") == "text"
        ]
        if any(
            isinstance(part, dict) and part.get("kind") == "data"
            and not isinstance(part.get("data"), dict)
            for part in parts
        ):
            return self._failed(
                request, "remote agent returned a data part that is not an object",
                task_id=task_id, state=state,
            )
        if wants_structure and not data_parts:
            # Typed output was requested.  Text is not quietly parsed or repaired
            # into JSON: a missing structure is a provider failure.
            return self._failed(
                request,
                "expected a structured DataPart for the declared output schema "
                "but the agent returned text only",
                task_id=task_id,
                state=state,
            )
        try:
            typed_outputs = self._typed_outputs(data_parts, text_parts, declared)
        except A2AProtocolError as exc:
            return self._failed(request, str(exc), task_id=task_id, state=state)
        return DelegationResult(
            invocation_id=request.invocation_id,
            status=DelegationStatus.COMPLETED,
            typed_outputs=typed_outputs,
            artifacts=[{"kind": "a2a_part", "part": part} for part in parts],
            external_run_id=task_id,
            metadata={"a2a_state": state, "a2a_task_id": task_id},
        )

    @staticmethod
    def _typed_outputs(
        data_parts: list[dict], text_parts: list[str], declared: list[str]
    ) -> list[dict]:
        """Map A2A parts onto the declared typed-output contract.

        A remote agent may return already-typed outputs.  When it returns a
        bare object and the definition declares exactly one output type, the
        type is unambiguous and is applied; when it is ambiguous, that is an
        error rather than a guess.
        """
        outputs: list[dict] = []
        for data in data_parts:
            if isinstance(data.get("typed_outputs"), list):
                for item in data["typed_outputs"]:
                    if not isinstance(item, dict) or not item.get("type"):
                        raise A2AProtocolError("every typed output must declare a type")
                    outputs.append(item)
                continue
            if data.get("type"):
                outputs.append(data)
                continue
            if len(declared) == 1:
                outputs.append({"type": declared[0], "value": data})
                continue
            raise A2AProtocolError(
                "remote agent returned untyped data and the definition declares "
                f"{len(declared)} output types, so its type is ambiguous"
            )
        if outputs or not text_parts:
            return outputs
        if len(declared) == 1:
            return [{"type": declared[0], "value": "\n".join(text_parts).strip()}]
        return []

    @staticmethod
    def _failed(
        request: DelegationRequest, error: str, *, task_id: str | None, state: str = ""
    ) -> DelegationResult:
        return DelegationResult(
            invocation_id=request.invocation_id,
            status=DelegationStatus.FAILED,
            error=error,
            external_run_id=task_id,
            metadata={"a2a_state": state, "a2a_task_id": task_id},
        )


def a2a_provider_record(
    name: str,
    endpoint: A2AEndpoint,
    *,
    version: str = "1",
    declared_permissions: tuple[str, ...] = (),
    priority: int = 0,
    trust_level: float = 0.5,
    agent_card: A2AAgentCard | None = None,
    metadata: dict | None = None,
) -> ExecutionProvider:
    """Build the ExecutionProvider record for one remote A2A agent.

    The URL lives here, in operational provider data — never in a Skill, a
    Capability or any Core model.
    """
    record_metadata = {
        "transport": "a2a",
        "url": endpoint.url,
        "poll_interval_seconds": endpoint.poll_interval_seconds,
        "timeout_seconds": endpoint.timeout_seconds,
        **(metadata or {}),
    }
    if endpoint.token_env:
        record_metadata["token_env"] = endpoint.token_env
    if agent_card is not None:
        record_metadata["agent_card"] = agent_card.to_dict()
    return ExecutionProvider(
        name=name,
        version=version,
        kind=ProviderKind.EXTERNAL_AGENT,
        adapter_name=f"a2a:{name}:{version}",
        status=ProviderStatus.ACTIVE,
        health=ProviderHealth.UNKNOWN,
        declared_permissions=tuple(declared_permissions),
        priority=priority,
        trust_level=trust_level,
        metadata=record_metadata,
    )
