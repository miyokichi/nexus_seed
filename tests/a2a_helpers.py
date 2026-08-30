"""A fake A2A agent for protocol-boundary tests.

Deliberately a real HTTP server speaking JSON-RPC rather than a stubbed client:
these tests exist to prove the *protocol* boundary, so no particular agent
product is a test dependency.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CARD = {
    "name": "fake-observer",
    "description": "A deterministic A2A agent for tests",
    "version": "1.0.0",
    "preferredTransport": "JSONRPC",
    "skills": [{"id": "interpret", "name": "Interpret"}],
}


def task(
    state: str,
    *,
    task_id: str = "task-1",
    artifacts=None,
    status_message=None,
) -> dict:
    """Build one A2A Task object in ``state``."""
    status: dict = {"state": state}
    if status_message is not None:
        status["message"] = {
            "kind": "message",
            "role": "agent",
            "parts": [{"kind": "text", "text": status_message}],
        }
    payload = {"kind": "task", "id": task_id, "contextId": "ctx-1", "status": status}
    if artifacts is not None:
        payload["artifacts"] = artifacts
    return payload


def data_artifact(data: dict, *, name: str = "result") -> dict:
    """An artifact carrying one DataPart."""
    return {"artifactId": "a-1", "name": name, "parts": [{"kind": "data", "data": data}]}


def text_artifact(text: str, *, name: str = "result") -> dict:
    """An artifact carrying one TextPart."""
    return {"artifactId": "a-1", "name": name, "parts": [{"kind": "text", "text": text}]}


def rpc_error(message: str) -> dict:
    """A scripted JSON-RPC error response."""
    return {"__rpc_error__": message}


def http_status(code: int) -> dict:
    """A scripted raw HTTP failure."""
    return {"__http_status__": code}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/.well-known/agent-card.json":
            if self.server.card is None:
                self._send(404, {"error": "no agent card here"})
            else:
                self._send(200, self.server.card)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "bad json"})
            return
        method = str(body.get("method") or "")
        self.server.requests.append(
            {
                "method": method,
                "params": body.get("params") or {},
                "authorization": self.headers.get("Authorization"),
            }
        )
        scripted = self.server.next_response(method)
        if scripted is None:
            self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
            return
        if "__http_status__" in scripted:
            self._send(scripted["__http_status__"], {"error": "scripted failure"})
            return
        if "__rpc_error__" in scripted:
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32000, "message": scripted["__rpc_error__"]},
                },
            )
            return
        self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": scripted})

    def log_message(self, *args) -> None:  # pragma: no cover - silence test output
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeA2AServer:
    """A scripted A2A agent listening on a loopback port.

    ``responses`` maps a JSON-RPC method to a list of scripted results; the
    last entry repeats, so a poll loop can be fed ``working`` until it gives up.
    """

    def __init__(self, responses: dict | None = None, *, card: dict | None = DEFAULT_CARD):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.card = card
        self._server.requests = []
        self._server.responses = {
            method: list(entries) for method, entries in (responses or {}).items()
        }
        self._server.next_response = self._next_response
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _next_response(self, method: str):
        entries = self._server.responses.get(method)
        if not entries:
            return None
        return entries.pop(0) if len(entries) > 1 else entries[0]

    def __enter__(self) -> FakeA2AServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list:
        return self._server.requests

    def calls(self, method: str) -> list:
        """Return every recorded request for one JSON-RPC method."""
        return [item for item in self.requests if item["method"] == method]


def free_url() -> str:
    """Return a loopback URL that nothing is listening on."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    host, port = server.server_address[:2]
    server.server_close()
    return f"http://{host}:{port}"
