"""Generic webhook adapter — the outside world pushes an occurrence to us.

Three layers, deliberately separable:

* :class:`WebhookAdapter` — body dict -> :class:`IngressEnvelope`.  No transport.
* :class:`WebhookIngress` — auth, adapter lookup, ingest, response shape.  Still
  no transport, so the whole contract is testable without a socket.
* :class:`WebhookServer` — a minimal asyncio HTTP/1.1 endpoint over it.

The server transport is stdlib-only on purpose: a web framework would be a
large amount of machinery for these small HTTP routes.  It is
an acceptance-grade endpoint, not a production deployment (spec §77).

**A webhook never waits for the work it causes** (spec §29).  The HTTP request
returns as soon as the Event is durable; interpretation, work discovery and any
resulting action run afterwards on the runtime, exactly as they would for an
event from any other source.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from ..core.event import utcnow
from ..ingress.models import IngressEnvelope, IngressStatus

logger = logging.getLogger("nexus_seed.adapters.webhook")

DEFAULT_ADAPTER_ID = "webhook"


class WebhookAdapter:
    """Turns a delivered JSON body into an envelope.

    ``source_event_key`` comes from the body — that is the provider's delivery
    id, and only the provider can say whether two POSTs are the same delivery
    (Invariant 30).  A body without one is refused downstream by validation
    rather than being given a made-up key here, because an invented key would
    make every redelivery look new.
    """

    source_type = "webhook"

    def __init__(self, adapter_id: str = DEFAULT_ADAPTER_ID) -> None:
        self._adapter_id = adapter_id

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def envelope(self, body: dict) -> IngressEnvelope:
        """Build an envelope from a webhook request body."""
        payload = body.get("payload")
        return IngressEnvelope(
            adapter_id=self._adapter_id,
            source_type=self.source_type,
            source_event_key=str(body.get("source_event_key") or ""),
            event_type=str(body.get("event_type") or ""),
            payload=payload if isinstance(payload, dict) else {},
            observed_at=_parse_time(body.get("observed_at")),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )


def _parse_time(value) -> datetime:
    """Parse an optional ISO timestamp, falling back to now (spec §27)."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return utcnow()


@dataclass
class WebhookResponse:
    """What the endpoint answers, independent of HTTP plumbing."""

    status_code: int
    body: Any = field(default_factory=dict)
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


class WebhookIngress:
    """Auth + ingest for pushed deliveries, with no transport attached.

    Deliveries are processed one at a time behind a lock.  A single SQLite
    connection and interleaved process activations do not mix, and serialising
    here costs nothing at webhook volumes.
    """

    def __init__(
        self,
        ingress_service,
        *,
        token: str | None = None,
        adapters: dict[str, WebhookAdapter] | None = None,
        default_adapter: WebhookAdapter | None = None,
    ) -> None:
        self.ingress = ingress_service
        self.token = token
        self.adapters = dict(adapters or {})
        self.default_adapter = default_adapter or WebhookAdapter()
        self._lock = asyncio.Lock()
        self._pending: set[asyncio.Task] = set()

    def adapter_for(self, adapter_id: str) -> WebhookAdapter:
        """Return the adapter serving ``adapter_id`` (created on demand)."""
        if adapter_id not in self.adapters:
            self.adapters[adapter_id] = (
                self.default_adapter
                if adapter_id == self.default_adapter.adapter_id
                else WebhookAdapter(adapter_id)
            )
        return self.adapters[adapter_id]

    def authorize(self, presented: str | None) -> bool:
        """Check the shared secret (spec §28).

        Configuring no token leaves the endpoint open, which is only ever
        appropriate for a closed test fixture — hence the warning.
        """
        if self.token is None:
            logger.warning("webhook ingress is running without a token")
            return True
        if not presented:
            return False
        return hmac.compare_digest(presented, self.token)

    async def handle(
        self, adapter_id: str, body: object, *, token: str | None = None
    ) -> WebhookResponse:
        """Process one delivery and answer immediately.

        The response reports whether the delivery was *ingested*, not whether
        anything it triggered has finished.
        """
        if not self.authorize(token):
            logger.warning("webhook delivery to %s rejected: bad token", adapter_id)
            return WebhookResponse(401, {"error": "unauthorized"})

        if not isinstance(body, dict):
            return WebhookResponse(400, {"error": "body must be a JSON object"})

        envelope = self.adapter_for(adapter_id).envelope(body)

        async with self._lock:
            result = await self.ingress.ingest(envelope, deliver=False)

        if result.status is IngressStatus.REJECTED:
            return WebhookResponse(
                400, {"error": "invalid envelope", "reasons": result.reasons}
            )

        if result.status is IngressStatus.DUPLICATE:
            # A redelivery is not an error — the provider did its job.  Report
            # success so it stops retrying, and say plainly that we already
            # had it (spec §30).
            return WebhookResponse(
                200,
                {
                    "status": "duplicate",
                    "duplicate": True,
                    "receipt_id": str(result.receipt.id),
                    "event_id": str(result.receipt.event_id) if result.receipt.event_id else None,
                },
            )

        self._schedule(result.event)
        return WebhookResponse(
            202,
            {
                "status": "accepted",
                "duplicate": False,
                "receipt_id": str(result.receipt.id),
                "event_id": str(result.event.id),
            },
        )

    def _schedule(self, event) -> None:
        """Run the event through the runtime after the response is sent."""

        async def deliver() -> None:
            async with self._lock:
                try:
                    await self.ingress.runtime.deliver_event(event)
                except Exception:  # noqa: BLE001 - a delivery must not kill the server
                    logger.exception("processing ingested event %s failed", event.id)

        task = asyncio.create_task(deliver())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain_pending(self) -> None:
        """Wait for every scheduled delivery to finish (tests, shutdown)."""
        while self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)


class WebhookServer:
    """A minimal asyncio HTTP endpoint: ``POST /ingress/{adapter_id}``.

    Single event loop, so it shares the runtime's SQLite connection safely.
    """

    def __init__(
        self,
        ingress: WebhookIngress,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        console=None,
        control_identity_id: str = "local-operator",
        cockpit=None,
    ) -> None:
        self.ingress = ingress
        self.host = host
        self.port = port
        self.console = console
        self.control_identity_id = control_identity_id
        self.cockpit = cockpit
        self._server: asyncio.AbstractServer | None = None

    @property
    def bound_port(self) -> int:
        """The port actually bound (meaningful when ``port=0`` was requested)."""
        if self._server is None:
            raise RuntimeError("server is not started")
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> "WebhookServer":
        """Begin listening."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("webhook server listening on %s:%d", self.host, self.bound_port)
        return self

    async def stop(self) -> None:
        """Stop listening and finish any in-flight processing."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.ingress.drain_pending()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._respond_to(reader)
        except Exception:  # noqa: BLE001 - never let one request kill the server
            logger.exception("webhook request failed")
            response = WebhookResponse(500, {"error": "internal error"})
        try:
            writer.write(_http_response(response))
            await writer.drain()
        finally:
            writer.close()

    async def _respond_to(self, reader: asyncio.StreamReader) -> WebhookResponse:
        request_line = await reader.readline()
        if not request_line:
            return WebhookResponse(400, {"error": "empty request"})
        parts = request_line.decode("latin-1").split()
        if len(parts) < 2:
            return WebhookResponse(400, {"error": "malformed request line"})
        method, path = parts[0], parts[1]

        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()

        length = int(headers.get("content-length") or 0)
        raw_body = await reader.readexactly(length) if length else b""

        clean_path = path.split("?", 1)[0]
        if method.upper() == "GET" and clean_path.startswith("/cockpit"):
            return self._cockpit_response(clean_path, headers)
        if method.upper() == "GET" and (
            clean_path == "/projects" or clean_path.startswith("/projects/")
        ):
            return self._project_response(clean_path, headers)
        if method.upper() != "POST":
            return WebhookResponse(405, {"error": "only POST is supported outside Cockpit"})
        if clean_path == "/control":
            if self.console is None:
                return WebhookResponse(404, {"error": "control endpoint is disabled"})
            if not self.ingress.authorize(_token_from(headers)):
                return WebhookResponse(401, {"error": "unauthorized"})
            try:
                body = json.loads(raw_body or b"{}")
            except ValueError:
                return WebhookResponse(400, {"error": "body is not valid JSON"})
            if not isinstance(body, dict) or not isinstance(body.get("command"), str):
                return WebhookResponse(400, {"error": "command must be a string"})
            try:
                result = self.console.execute_text(
                    body["command"],
                    issuer_identity_id=self.control_identity_id,
                    source_channel=str(body.get("source_channel") or "http-control"),
                    source_message_id=str(body.get("source_message_id") or "") or None,
                    idempotency_key=str(body.get("idempotency_key") or "") or None,
                )
            except ValueError as exc:
                return WebhookResponse(400, {"error": str(exc)})
            return WebhookResponse(200, result.to_dict())
        adapter_id = _adapter_id_from_path(path)
        if adapter_id is None:
            return WebhookResponse(404, {"error": "unknown path"})

        try:
            body = json.loads(raw_body or b"{}")
        except ValueError:
            return WebhookResponse(400, {"error": "body is not valid JSON"})

        return await self.ingress.handle(adapter_id, body, token=_token_from(headers))

    def _cockpit_response(
        self, path: str, headers: dict[str, str]
    ) -> WebhookResponse:
        """Serve the optional Human Interface without changing Runtime state."""

        if self.cockpit is None:
            return WebhookResponse(404, {"error": "cockpit is disabled"})
        security_headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self'; frame-ancestors 'none'"
            ),
        }
        if path in {"/cockpit", "/cockpit/", "/cockpit/index.html"}:
            from ..cockpit.assets import INDEX_HTML

            return WebhookResponse(
                200, INDEX_HTML, "text/html; charset=utf-8", security_headers
            )
        if path == "/cockpit/styles.css":
            from ..cockpit.assets import STYLES_CSS

            return WebhookResponse(
                200, STYLES_CSS, "text/css; charset=utf-8", security_headers
            )
        if path == "/cockpit/app.js":
            from ..cockpit.assets import APP_JS

            return WebhookResponse(
                200, APP_JS, "text/javascript; charset=utf-8", security_headers
            )
        if path == "/cockpit/api/snapshot":
            if not self.ingress.authorize(_token_from(headers)):
                return WebhookResponse(401, {"error": "unauthorized"})
            return WebhookResponse(200, self.cockpit.snapshot(), headers=security_headers)
        return WebhookResponse(404, {"error": "unknown cockpit path"})

    def _project_response(
        self, path: str, headers: dict[str, str]
    ) -> WebhookResponse:
        """Serve authenticated read-only Project Situation projections."""

        if self.cockpit is None:
            return WebhookResponse(404, {"error": "cockpit is disabled"})
        security_headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if not self.ingress.authorize(_token_from(headers)):
            return WebhookResponse(
                401, {"error": "unauthorized"}, headers=security_headers
            )
        if path == "/projects":
            return WebhookResponse(
                200, {"projects": self.cockpit.projects()}, headers=security_headers
            )
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "projects" and parts[2] == "situation":
            project_id = unquote(parts[1])
            situation = self.cockpit.project_situation(project_id)
            if situation is None:
                return WebhookResponse(
                    404, {"error": "project not found"}, headers=security_headers
                )
            return WebhookResponse(200, situation, headers=security_headers)
        return WebhookResponse(
            404, {"error": "unknown project path"}, headers=security_headers
        )


def _adapter_id_from_path(path: str) -> str | None:
    """Extract ``{adapter_id}`` from ``/ingress/{adapter_id}``."""
    clean = path.split("?", 1)[0].strip("/")
    parts = clean.split("/")
    if len(parts) == 2 and parts[0] == "ingress" and parts[1]:
        return parts[1]
    return None


def _token_from(headers: dict[str, str]) -> str | None:
    """Read the shared secret from ``Authorization: Bearer`` or ``X-Ingress-Token``."""
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return headers.get("x-ingress-token")


def _http_response(response: WebhookResponse) -> bytes:
    if isinstance(response.body, bytes):
        body = response.body
    elif isinstance(response.body, str) and not response.content_type.startswith(
        "application/json"
    ):
        body = response.body.encode("utf-8")
    else:
        body = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
    reason = {
        200: "OK",
        202: "Accepted",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(response.status_code, "OK")
    head = (
        f"HTTP/1.1 {response.status_code} {reason}\r\n"
        f"Content-Type: {response.content_type}\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in response.headers.items())
        + f"Content-Length: {len(body)}\r\n"
        + "Connection: close\r\n\r\n"
    )
    return head.encode("latin-1") + body
