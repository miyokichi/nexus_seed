"""A2A client: discover a remote agent and drive a task to completion.

Uses only the standard library (``urllib``), matching the rest of Little Agent.
Works against any A2A-compliant agent, not just another Little Agent.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from little_agent.a2a.grant import WorkGrant
from little_agent.a2a.models import (
    AGENT_CARD_PATH,
    DEPTH_METADATA_KEY,
    LEGACY_AGENT_CARD_PATH,
    TERMINAL_STATES,
    new_id,
    new_message,
)

DEFAULT_TIMEOUT = 300.0
POLL_INTERVAL = 0.5


class A2AClientError(Exception):
    """A transport, protocol, or remote-side failure while talking to a peer."""


def _request(url: str, payload: dict[str, Any] | None, token: str | None, timeout: float) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise A2AClientError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise A2AClientError(f"Could not reach {url}: {exc.reason}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise A2AClientError(f"Peer returned invalid JSON: {body[:200]}") from exc


def fetch_agent_card(base_url: str, token: str | None = None, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch a peer's Agent Card, trying the canonical then the legacy path."""

    root = base_url if base_url.endswith("/") else base_url + "/"
    errors: list[str] = []
    for path in (AGENT_CARD_PATH, LEGACY_AGENT_CARD_PATH):
        try:
            card = _request(urljoin(root, path.lstrip("/")), None, token, timeout)
        except A2AClientError as exc:
            errors.append(str(exc))
            continue
        if isinstance(card, dict) and card.get("name"):
            return card
        errors.append(f"{path} did not return an Agent Card")
    raise A2AClientError("No Agent Card found at " + base_url + ": " + "; ".join(errors))


class A2AClient:
    """Minimal JSON-RPC 2.0 client for one A2A peer."""

    def __init__(self, card: dict[str, Any], token: str | None = None, timeout: float = 60.0) -> None:
        self.card = card
        self.token = token
        self.timeout = timeout
        endpoint = str(card.get("url") or "")
        if not urlparse(endpoint).scheme:
            raise A2AClientError(f"Agent Card has no usable 'url': {endpoint!r}")
        self.endpoint = endpoint

    @classmethod
    def connect(cls, base_url: str, token: str | None = None, timeout: float = 60.0) -> "A2AClient":
        return cls(fetch_agent_card(base_url, token), token=token, timeout=timeout)

    @property
    def name(self) -> str:
        return str(self.card.get("name") or "unknown")

    def call(self, method: str, params: dict[str, Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": new_id(), "method": method, "params": params}
        response = _request(self.endpoint, payload, self.token, self.timeout)
        if not isinstance(response, dict):
            raise A2AClientError("Peer returned a non-object JSON-RPC response.")
        if "error" in response:
            error = response["error"] or {}
            code = error.get("code")
            message = error.get("message") or "unknown error"
            raise A2AClientError(f"Peer returned error {code}: {message}")
        if "result" not in response:
            raise A2AClientError("Peer returned neither result nor error.")
        return response["result"]

    def send_message(
        self,
        text: str = "",
        depth: int | None = None,
        data: Any = None,
        grant: WorkGrant | None = None,
    ) -> dict[str, Any]:
        """Send one message. ``data`` is sent as an A2A DataPart alongside the text."""

        metadata: dict[str, Any] = {}
        if depth is not None:
            metadata[DEPTH_METADATA_KEY] = depth
        if grant is not None and not grant.is_empty:
            # Where the peer should work: its workspace and any granted paths.
            metadata.update(grant.to_metadata())
        message = new_message("user", text, data=data, metadata=metadata or None)
        result = self.call("message/send", {"message": message})
        if not isinstance(result, dict):
            raise A2AClientError("message/send did not return a Task or Message.")
        return result

    def get_task(self, task_id: str) -> dict[str, Any]:
        result = self.call("tasks/get", {"id": task_id})
        if not isinstance(result, dict):
            raise A2AClientError("tasks/get did not return a Task.")
        return result

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        result = self.call("tasks/cancel", {"id": task_id})
        if not isinstance(result, dict):
            raise A2AClientError("tasks/cancel did not return a Task.")
        return result

    def run_task(
        self,
        text: str = "",
        depth: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        should_stop: Callable[[], bool] | None = None,
        data: Any = None,
        grant: WorkGrant | None = None,
    ) -> dict[str, Any]:
        """Send a message and poll until the task reaches a terminal state.

        ``data`` is sent as a DataPart, which is how structured input (and an
        ``output_schema`` asking for a structured result) reaches the peer.

        A peer may answer ``message/send`` with a Message instead of a Task (the
        spec allows it for immediate replies); that is returned as-is.

        ``should_stop`` is polled between attempts so a caller (the emergency-stop
        hotkey, or a cancelled parent task) can abandon the wait; the peer's task
        is cancelled rather than left running.

        ``grant`` optionally asks the peer to work in a particular workspace and
        to reach given paths outside it. The peer authorizes it against its own
        configuration and refuses anything it may not touch.
        """

        if should_stop is not None and should_stop():
            raise A2AClientError("Stopped before the task was sent.")

        result = self.send_message(text, depth=depth, data=data, grant=grant)
        if result.get("kind") == "message":
            return result

        task_id = str(result.get("id") or "")
        if not task_id:
            raise A2AClientError("Peer returned a Task without an id.")

        deadline = time.monotonic() + timeout
        task = result
        while str((task.get("status") or {}).get("state")) not in TERMINAL_STATES:
            if should_stop is not None and should_stop():
                self._cancel_quietly(task_id)
                raise A2AClientError(f"Stopped by the caller; peer task {task_id} canceled.")
            if time.monotonic() >= deadline:
                # Be a good citizen: ask the peer to stop the work we abandoned.
                self._cancel_quietly(task_id)
                raise A2AClientError(
                    f"Task {task_id} did not finish within {timeout:.0f}s (canceled)."
                )
            time.sleep(poll_interval)
            task = self.get_task(task_id)
        return task

    def _cancel_quietly(self, task_id: str) -> None:
        try:
            self.cancel_task(task_id)
        except A2AClientError:
            pass
