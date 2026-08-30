"""Delegate subtasks to peer agents over the A2A (Agent2Agent) protocol.

Two tools share one code path:

- ``delegate_task`` — hand one subtask to a peer and wait for its result.
- ``delegate_tasks`` — fan several independent subtasks out **concurrently**
  (each its own A2A task, optionally on different peers) and collect them.

A subtask can also say *where* it happens: ``workspace`` is the directory the
peer works in (read and write) and ``allowed_paths`` lists reference files and
directories outside it the peer may read. Each is checked against the matching
half of what this agent can reach — writable for the workspace, readable for the
allowed paths — before anything is sent, and again by the peer on arrival (see
:mod:`little_agent.a2a.grant`).

Both are A2A **clients**: discover the peer's Agent Card, send ``message/send``,
poll ``tasks/get`` to a terminal state, and return the artifact text. The peer
can be any A2A-compliant agent reachable by URL, or a local ``agents/`` profile
whose server is started on demand (see ``little_agent.a2a.peers``). Delegation
depth travels in the message metadata so hand-off chains cannot recurse forever.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from little_agent.a2a.client import A2AClientError
from little_agent.a2a.grant import GrantError, GrantPolicy, WorkGrant
from little_agent.a2a.models import TASK_COMPLETED, task_result_text
from little_agent.a2a.peers import PeerPool, shared_pool
from little_agent.config import AgentConfig
from little_agent.tools.base import ToolContext, ToolResult

DEFAULT_TASK_TIMEOUT = 300.0

_TASK_DESCRIPTION = (
    "Full, self-contained instructions for the peer agent. It does not share this "
    "conversation, so include every detail it needs to finish on its own."
)
_AGENT_DESCRIPTION = (
    "Peer to delegate to: a local agent profile name or a configured remote peer name. "
    "Omit for the default library agent."
)
_AGENT_URL_DESCRIPTION = (
    "Base URL of an A2A agent to delegate to (e.g. http://127.0.0.1:8801/). "
    "Overrides 'agent'. Use for peers not in the configured list."
)
_BACKGROUND_DESCRIPTION = "Optional background or source material to send before the task."
_WORKSPACE_DESCRIPTION = (
    "Optional directory the peer should treat as its workspace for this subtask, where "
    "it reads and writes. Relative to your own workspace, or an absolute path you can "
    "write yourself. The peer's output belongs here."
)
_ALLOWED_PATHS_DESCRIPTION = (
    "Optional reference files or directories OUTSIDE that workspace the peer may READ "
    "(not write). You can only hand over paths you can read yourself, and the peer "
    "checks them again."
)


@dataclass(slots=True)
class _Outcome:
    """One finished delegation, ready to be rendered for the model."""

    label: str
    ok: bool
    text: str


class _DelegationRunner:
    """Shared A2A delegation logic for the singular and parallel tools."""

    def __init__(
        self,
        config: AgentConfig,
        depth: int,
        pool: PeerPool,
        stop: Any | None,
        timeout: float,
    ) -> None:
        self._config = config
        self._depth = depth
        self._pool = pool
        self._stop = stop
        self._timeout = timeout
        # What this agent may hand on: a workspace only from what it can write,
        # reference paths only from what it can read. A delegation can never
        # widen its own reach.
        self._policy = GrantPolicy.from_config(config)

    @property
    def depth_exceeded(self) -> bool:
        return self._depth >= self._config.max_delegation_depth

    def depth_error(self) -> str:
        return (
            f"Delegation depth limit ({self._config.max_delegation_depth}) reached. "
            "Do this work directly instead of delegating further."
        )

    def peers(self) -> list[str]:
        try:
            return self._pool.available()
        except Exception:  # noqa: BLE001 - never let peer discovery break a description.
            return []

    def _should_stop(self) -> bool:
        return bool(self._stop is not None and self._stop.triggered)

    def run_one(self, spec: dict[str, Any]) -> _Outcome:
        task_text = str(spec.get("task") or "").strip()
        agent_name = str(spec.get("agent") or "").strip() or None
        agent_url = str(spec.get("agent_url") or "").strip() or None
        background = str(spec.get("background") or "").strip()
        label = agent_url or agent_name or "default"

        if not task_text:
            return _Outcome(label, False, "task is required.")

        try:
            grant = self._policy.authorize(
                WorkGrant.request(
                    self._config.workspace,
                    workspace=spec.get("workspace"),
                    allowed_paths=spec.get("allowed_paths"),
                )
            )
        except GrantError as exc:
            # Refused before anything is sent: the peer never sees a path this
            # agent had no business handing over.
            return _Outcome(label, False, f"Cannot hand over that work directory: {exc}")

        try:
            client = self._pool.connect(name=agent_name, url=agent_url)
        except FileNotFoundError:
            available = ", ".join(self.peers()) or "(none)"
            return _Outcome(
                label,
                False,
                f"No such agent profile: {agent_name}. Available peers: {available}. "
                "Omit 'agent' to use the default library agent.",
            )
        except A2AClientError as exc:
            return _Outcome(label, False, f"Could not connect to the peer agent: {exc}")
        except Exception as exc:  # noqa: BLE001 - reported as a failed subtask.
            return _Outcome(label, False, f"Could not reach the peer agent: {exc}")

        prompt = (
            task_text if not background else f"Background:\n{background}\n\n---\nTask:\n{task_text}"
        )
        try:
            result = client.run_task(
                prompt,
                depth=self._depth + 1,
                timeout=self._timeout,
                should_stop=self._should_stop,
                grant=grant,
            )
        except A2AClientError as exc:
            return _Outcome(label, False, f"A2A task failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - reported as a failed subtask.
            return _Outcome(label, False, f"A2A task failed: {exc}")

        label = agent_url or agent_name or client.name
        text = task_result_text(result) or "(peer returned no content)"
        if result.get("kind") == "message":
            return _Outcome(label, True, text)
        state = str((result.get("status") or {}).get("state") or "unknown")
        if state != TASK_COMPLETED:
            return _Outcome(label, False, f"task {state}: {text}")
        return _Outcome(label, True, text)

    def run_many(self, specs: list[dict[str, Any]]) -> list[_Outcome]:
        """Run subtasks concurrently, preserving the caller's ordering in the results."""

        workers = min(len(specs), max(1, self._config.max_parallel_delegations))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="a2a-delegate") as pool:
            return list(pool.map(self.run_one, specs))


def _peer_hint(peers: list[str]) -> str:
    return " Available peers: " + ", ".join(peers) + "." if peers else ""


class DelegateTaskTool:
    name = "delegate_task"
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": _TASK_DESCRIPTION},
            "agent": {"type": "string", "description": _AGENT_DESCRIPTION},
            "agent_url": {"type": "string", "description": _AGENT_URL_DESCRIPTION},
            "background": {"type": "string", "description": _BACKGROUND_DESCRIPTION},
            "workspace": {"type": "string", "description": _WORKSPACE_DESCRIPTION},
            "allowed_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": _ALLOWED_PATHS_DESCRIPTION,
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        config: AgentConfig,
        depth: int = 0,
        pool: PeerPool | None = None,
        stop: Any | None = None,
        timeout: float = DEFAULT_TASK_TIMEOUT,
    ) -> None:
        self._runner = _DelegationRunner(
            config, depth, pool or shared_pool(config), stop, timeout
        )
        self.description = (
            "Delegate ONE self-contained subtask to a peer agent over the A2A (Agent2Agent) "
            "protocol and return its result. The peer runs the task independently with its own "
            "context, so put everything it needs in 'task'. For several independent subtasks at "
            "once, use delegate_tasks instead — it runs them in parallel."
            + _peer_hint(self._runner.peers())
        )

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        if not str(kwargs.get("task") or "").strip():
            return ToolResult(False, "task is required.")
        if self._runner.depth_exceeded:
            return ToolResult(False, self._runner.depth_error())

        outcome = self._runner.run_one(kwargs)
        if not outcome.ok:
            return ToolResult(False, f"[A2A peer '{outcome.label}'] {outcome.text}")
        return ToolResult(True, f"[A2A peer '{outcome.label}' result]\n{outcome.text}")


class DelegateTasksTool:
    name = "delegate_tasks"
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Independent subtasks to run at the same time. Only use this when the "
                    "subtasks do not depend on each other's results."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": _TASK_DESCRIPTION},
                        "agent": {"type": "string", "description": _AGENT_DESCRIPTION},
                        "agent_url": {"type": "string", "description": _AGENT_URL_DESCRIPTION},
                        "background": {
                            "type": "string",
                            "description": _BACKGROUND_DESCRIPTION,
                        },
                        "workspace": {
                            "type": "string",
                            "description": _WORKSPACE_DESCRIPTION,
                        },
                        "allowed_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": _ALLOWED_PATHS_DESCRIPTION,
                        },
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        config: AgentConfig,
        depth: int = 0,
        pool: PeerPool | None = None,
        stop: Any | None = None,
        timeout: float = DEFAULT_TASK_TIMEOUT,
    ) -> None:
        self._config = config
        self._runner = _DelegationRunner(
            config, depth, pool or shared_pool(config), stop, timeout
        )
        self.description = (
            "Delegate SEVERAL independent subtasks to peer agents over the A2A (Agent2Agent) "
            "protocol, running them in parallel, and return all results together. Use it to fan "
            "work out — e.g. research three topics at once, or send different subtasks to "
            "different specialized agents. Each subtask runs with its own context, so put "
            "everything it needs in its 'task'. Subtasks that depend on each other must not go "
            f"in one call. Up to {config.max_parallel_delegations} run concurrently."
            + _peer_hint(self._runner.peers())
        )

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        raw = kwargs.get("tasks")
        if not isinstance(raw, list) or not raw:
            return ToolResult(False, "tasks must be a non-empty array of subtasks.")
        specs = [item for item in raw if isinstance(item, dict)]
        if len(specs) != len(raw):
            return ToolResult(False, "Every entry in tasks must be an object with a 'task' field.")
        if self._runner.depth_exceeded:
            return ToolResult(False, self._runner.depth_error())

        outcomes = self._runner.run_many(specs)
        total = len(outcomes)
        succeeded = sum(1 for outcome in outcomes if outcome.ok)

        blocks: list[str] = []
        for index, outcome in enumerate(outcomes, start=1):
            status = "completed" if outcome.ok else "FAILED"
            blocks.append(
                f"[{index}/{total}] peer '{outcome.label}' — {status}\n{outcome.text}"
            )
        header = f"Delegated {total} subtask(s) in parallel: {succeeded} completed, {total - succeeded} failed."
        content = header + "\n\n" + "\n\n".join(blocks)
        # Partial success still carries usable results, so only a total wipeout is an error.
        return ToolResult(succeeded > 0, content)
