"""Action backends — the only things in NEXUS SEED that touch the outside.

An action backend is the outbound twin of the Phase 3B LLM backend and obeys
the same rule (Invariant 19, restated as Invariant 25): it takes a request,
performs one external capability, and returns a result.  It does **not** judge
permissions, risk or approval, does not read or write World State, does not
create work, and does not retry on its own — every one of those belongs to a
Process upstream of it.

A backend also publishes :class:`BackendCapabilities`: which ``action_type``s
it can perform and, for each, the permissions the system *mandates* regardless
of what a proposal declares.  That registry is what makes permission escalation
by an under-declaring proposal impossible (spec §17).
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class ActionRequest:
    """Input to one action-backend call.

    Attributes:
        action_type: Which capability to run.
        target: What to act on (backend-specific; a path for file backends).
        parameters: Action arguments.
        idempotency_key: Logical identity of the side effect.  A backend that
            can de-duplicate should treat a repeated key as "already done" and
            perform no second effect (spec §34).
        metadata: Correlation ids for logs (proposal/execution/process).
    """

    action_type: str
    target: str | None = None
    parameters: dict = field(default_factory=dict)
    idempotency_key: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ActionResult:
    """Output of one action-backend call.

    ``retryable`` distinguishes a transient failure (timeout, lock) from a
    permanent one (sandbox violation, unknown action).  Only transient failures
    go back through the Phase 2A retry loop; a permanent one must not be
    hammered.
    """

    success: bool = True
    output: dict | None = None
    error: str | None = None
    retryable: bool = False
    duplicate: bool = False
    latency_ms: float | None = None


@dataclass(frozen=True)
class ActionCapability:
    """One action a backend can perform, and what it always requires.

    ``required_permissions`` are *mandatory*: the system demands them for this
    action type no matter what a proposal claims about itself.
    """

    action_type: str
    required_permissions: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackendCapabilities:
    """The deterministic registry of what a backend can do (spec §12).

    No semantic search, no discovery protocol — a dict lookup by action type.
    """

    backend: str
    actions: dict[str, ActionCapability] = field(default_factory=dict)

    def get(self, action_type: str) -> ActionCapability | None:
        """Return the capability for ``action_type``, or ``None`` if unsupported."""
        return self.actions.get(action_type)

    def supports(self, action_type: str) -> bool:
        """Return whether this backend can perform ``action_type``."""
        return action_type in self.actions


def capabilities_of(backend: object) -> BackendCapabilities | None:
    """Return a backend's capabilities, or ``None`` if it publishes none.

    A backend without capabilities cannot be used for actions: validation
    refuses it rather than guessing what it is allowed to do.
    """
    getter = getattr(backend, "capabilities", None)
    if getter is None:
        return None
    result = getter()
    return result if isinstance(result, BackendCapabilities) else None


@runtime_checkable
class ActionBackend(Protocol):
    """A backend that performs external actions."""

    def capabilities(self) -> BackendCapabilities:
        """Return the action types this backend can perform."""
        ...

    async def execute(self, request: ActionRequest) -> ActionResult:
        """Perform one action and return its result (never mutating state)."""
        ...


# --- fake backend ----------------------------------------------------------


FAKE_CAPABILITIES = BackendCapabilities(
    backend="fake_action",
    actions={
        "write_file": ActionCapability(
            "write_file", ("filesystem.write",), ("filesystem_write",)
        ),
        "read_file": ActionCapability("read_file", ("filesystem.read",), ()),
        "noop": ActionCapability("noop", (), ()),
    },
)


def action_success(output: dict | None = None) -> ActionResult:
    """A scripted successful action result."""
    return ActionResult(success=True, output=output or {"ok": True})


def action_failure(error: str, *, retryable: bool = True) -> ActionResult:
    """A scripted failed action result (transient by default)."""
    return ActionResult(success=False, error=error, retryable=retryable)


def action_timeout(error: str = "timeout") -> ActionResult:
    """A scripted timeout (a transient failure)."""
    return ActionResult(success=False, error=error, retryable=True)


class FakeActionBackend:
    """A deterministic, side-effect-free action backend for tests (spec §37).

    Scripts success / failure / timeout / success-after-failure, and detects
    duplicate calls: a repeated ``idempotency_key`` is answered from
    :attr:`performed` with ``duplicate=True`` and does not consume a script
    step, so tests can prove the side effect happened only once.
    """

    def __init__(
        self,
        script: list[ActionResult] | None = None,
        *,
        default: ActionResult | None = None,
        capabilities: BackendCapabilities | None = None,
    ) -> None:
        self.script = list(script or [])
        self.default = default
        self._capabilities = capabilities or FAKE_CAPABILITIES
        #: Every request the backend was asked to perform (including duplicates).
        self.calls: list[ActionRequest] = []
        #: Effects actually performed, by idempotency key.
        self.performed: dict[str, dict] = {}

    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    @property
    def effect_count(self) -> int:
        """How many *external effects* were performed (duplicates excluded)."""
        return len(self.performed)

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        key = request.idempotency_key
        if key is not None and key in self.performed:
            return ActionResult(
                success=True, output=dict(self.performed[key]), duplicate=True
            )

        attempt = len([c for c in self.calls if c.idempotency_key == key])
        if self.script:
            result = self.script[min(attempt - 1, len(self.script) - 1)]
        elif self.default is not None:
            result = self.default
        else:
            result = action_success({"action_type": request.action_type})

        if result.success and key is not None:
            self.performed[key] = dict(result.output or {})
        return result


# --- local file backend ----------------------------------------------------


LOCAL_FILE_CAPABILITIES = BackendCapabilities(
    backend="local_file",
    actions={
        "write_file": ActionCapability(
            "write_file", ("filesystem.write",), ("filesystem_write",)
        ),
        "read_file": ActionCapability("read_file", ("filesystem.read",), ()),
    },
)

#: Name of the durable idempotency journal kept inside ``allowed_root``.
JOURNAL_NAME = ".nexus_seed_action_journal.json"


class LocalFileActionBackend:
    """A real action backend: reads and writes files under one sandbox root.

    Two safety properties matter more than the feature set:

    * **Sandbox** — every target resolves under ``allowed_root``; anything that
      escapes it (absolute path elsewhere, ``..`` traversal, a symlink out) is a
      permanent, non-retryable failure and no file is touched (spec §39).
    * **Idempotency** — completed effects are journalled in ``allowed_root`` by
      idempotency key, so a repeat after a crash between "file written" and "DB
      committed" is answered from the journal instead of writing again
      (spec §34–§35).
    """

    def __init__(self, allowed_root: str | Path, *, name: str = "local_file") -> None:
        self.allowed_root = Path(allowed_root).resolve()
        self.allowed_root.mkdir(parents=True, exist_ok=True)
        self.name = name
        self._capabilities = BackendCapabilities(
            backend=name, actions=dict(LOCAL_FILE_CAPABILITIES.actions)
        )
        #: Requests received (for tests/observability); not a journal.
        self.calls: list[ActionRequest] = []

    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    # --- sandbox -----------------------------------------------------------

    def resolve(self, target: str | None) -> Path:
        """Resolve ``target`` inside the sandbox.

        Raises:
            ValueError: If ``target`` is empty or escapes ``allowed_root``.
        """
        if not target:
            raise ValueError("target is required")
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = self.allowed_root / candidate
        resolved = candidate.resolve()
        # Strictly *inside* the root: the root itself is not a valid target.
        if self.allowed_root not in resolved.parents:
            raise ValueError(
                f"target {target!r} escapes allowed_root {self.allowed_root}"
            )
        if resolved.name == JOURNAL_NAME:
            raise ValueError("the idempotency journal is not a writable target")
        return resolved

    # --- idempotency journal ----------------------------------------------

    @property
    def journal_path(self) -> Path:
        """Path of the durable idempotency journal."""
        return self.allowed_root / JOURNAL_NAME

    def _read_journal(self) -> dict:
        try:
            return json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_journal(self, journal: dict) -> None:
        # Write-then-replace so a crash cannot leave a truncated journal.
        tmp = self.journal_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(journal), encoding="utf-8")
        shutil.move(str(tmp), str(self.journal_path))

    # --- execution ---------------------------------------------------------

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        started = time.monotonic()

        capability = self._capabilities.get(request.action_type)
        if capability is None:
            return ActionResult(
                success=False,
                error=f"unsupported action_type {request.action_type!r}",
                retryable=False,
            )

        key = request.idempotency_key
        journal = self._read_journal() if key else {}
        if key and key in journal:
            return ActionResult(
                success=True, output=dict(journal[key]), duplicate=True
            )

        try:
            path = self.resolve(request.target)
        except ValueError as exc:
            # A sandbox violation is permanent: refuse, never retry.
            return ActionResult(success=False, error=str(exc), retryable=False)

        try:
            output = self._perform(request, path)
        except OSError as exc:
            return ActionResult(success=False, error=str(exc), retryable=True)

        if key:
            journal[key] = output
            self._write_journal(journal)

        return ActionResult(
            success=True, output=output, latency_ms=(time.monotonic() - started) * 1000.0
        )

    def _perform(self, request: ActionRequest, path: Path) -> dict:
        if request.action_type == "write_file":
            content = str(request.parameters.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {
                "path": str(path),
                "bytes_written": len(content.encode("utf-8")),
            }
        content = path.read_text(encoding="utf-8")
        return {"path": str(path), "content": content}


def new_action_id() -> uuid.UUID:  # pragma: no cover - trivial helper
    """Return a fresh id (kept here so backends need no uuid import)."""
    return uuid.uuid4()


def summarize(output: Any, *, limit: int = 200) -> dict:
    """Return a small, JSON-safe summary of a backend output for an Event.

    Action results go back into the system as *events*, not as state writes
    (Invariant 24), so the payload stays a summary — never something a
    downstream process could mistake for authoritative world state.
    """
    if not isinstance(output, dict):
        return {"value": str(output)[:limit]}
    summary: dict = {}
    for key, value in output.items():
        if isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        else:
            summary[key] = str(value)[:limit]
    return summary
