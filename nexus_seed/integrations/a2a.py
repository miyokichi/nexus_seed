"""Small standard-library client for the Project Agent A2A transport."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


AGENT_CARD_PATH = "/.well-known/agent-card.json"
TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
UNSUPPORTED_STATES = frozenset({"input-required", "auth-required"})
TASK_NOT_FOUND_CODE = -32001


class A2AProtocolError(RuntimeError):
    """The remote endpoint did not return a usable A2A response."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def task_not_found(self) -> bool:
        """Whether the remote runtime no longer knows the requested task."""
        return self.code == TASK_NOT_FOUND_CODE


@dataclass(frozen=True)
class A2AAgentCard:
    """The remote Agent Card fields used for connection diagnostics."""

    name: str = ""
    description: str = ""
    version: str = ""
    url: str = ""
    transport: str = ""
    skills: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> A2AAgentCard:
        """Parse the useful fields and retain the original card."""
        if not isinstance(data, dict):
            raise A2AProtocolError("agent card must be a JSON object")
        skills: list[str] = []
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
        """Return the stable diagnostic representation."""
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
    """Connection settings for one remote Project Agent runtime."""

    url: str
    token_env: str | None = None
    poll_interval_seconds: float = 1.0
    timeout_seconds: float = 300.0
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"A2A url must be an http(s) URL: {self.url!r}")
        for name in (
            "poll_interval_seconds",
            "timeout_seconds",
            "request_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @property
    def token(self) -> str | None:
        """Read the bearer token from its configured environment variable."""
        if not self.token_env:
            return None
        return os.environ.get(self.token_env, "").strip() or None


class A2AClient:
    """Blocking JSON-RPC client; async callers place calls in a worker thread."""

    def __init__(self, endpoint: A2AEndpoint) -> None:
        self.endpoint = endpoint
        self._card: A2AAgentCard | None = None

    def agent_card(self, *, refresh: bool = False) -> A2AAgentCard:
        """Fetch and cache the remote Agent Card."""
        if self._card is not None and not refresh:
            return self._card
        url = urljoin(
            self.endpoint.url.rstrip("/") + "/", AGENT_CARD_PATH.lstrip("/")
        )
        request = Request(url, method="GET", headers=self._headers())
        try:
            with urlopen(
                request, timeout=self.endpoint.request_timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise A2AProtocolError(f"agent card unavailable at {url}: {exc}") from exc
        self._card = A2AAgentCard.from_dict(payload)
        return self._card

    def call(self, method: str, params: dict) -> dict:
        """Perform one JSON-RPC call and return its result object."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": method,
                "params": params,
            }
        ).encode("utf-8")
        request = Request(
            self.endpoint.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", **self._headers()},
        )
        try:
            with urlopen(
                request, timeout=self.endpoint.request_timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise A2AProtocolError(f"{method} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise A2AProtocolError(f"{method} returned a non-object response")
        if payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else error
            code = error.get("code") if isinstance(error, dict) else None
            raise A2AProtocolError(
                f"{method} rejected: {message}",
                code=code if isinstance(code, int) else None,
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise A2AProtocolError(f"{method} returned no result object")
        return result

    def _headers(self) -> dict[str, str]:
        token = self.endpoint.token
        return {"Authorization": f"Bearer {token}"} if token else {}


def task_state(task: dict) -> str:
    """Return the lower-cased A2A task state."""
    status = task.get("status")
    if isinstance(status, dict):
        return str(status.get("state") or "").lower()
    return str(status or "").lower()


__all__ = [
    "AGENT_CARD_PATH",
    "A2AAgentCard",
    "A2AClient",
    "A2AEndpoint",
    "A2AProtocolError",
    "TASK_NOT_FOUND_CODE",
    "TERMINAL_STATES",
    "UNSUPPORTED_STATES",
    "task_state",
]
