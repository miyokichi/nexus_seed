"""Operational helpers for the human-facing NEXUS SEED CLI.

Write commands go through the webhook ingress boundary.  Inspection commands
open SQLite in read-only mode, so they can safely be used while the application
server owns the normal read/write connection.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from urllib import error, request
import uuid


class OperationalCommandError(RuntimeError):
    """Raised when an operational CLI command cannot be completed."""


@dataclass(frozen=True, slots=True)
class PendingReview:
    """A reviewable condition recovered from a durable Continuation."""

    continuation_id: str
    process_instance_id: str
    process_definition: str
    resume_point: str
    event_type: str
    match_fields: dict[str, Any]
    review_id: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable operator view."""

        return {
            "review_id": self.review_id,
            "review_type": self.event_type.removesuffix("_reviewed"),
            "event_type": self.event_type,
            "match_fields": self.match_fields,
            "process_definition": self.process_definition,
            "process_instance_id": self.process_instance_id,
            "continuation_id": self.continuation_id,
            "resume_point": self.resume_point,
            "created_at": self.created_at,
        }


def submit_webhook_event(
    *,
    host: str,
    port: int,
    token: str | None,
    event_type: str,
    payload: dict[str, Any],
    source_event_key: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Submit one external Event to a running NEXUS SEED webhook server."""

    event_type = event_type.strip()
    if not event_type:
        raise OperationalCommandError("event type must not be empty")
    key = source_event_key or f"cli-{event_type}-{uuid.uuid4()}"
    url = _webhook_url(host, port)
    body = json.dumps(
        {
            "source_event_key": key,
            "event_type": event_type,
            "payload": payload,
            "metadata": {"submitted_by": "nexus-seed-cli"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["X-Ingress-Token"] = token
    http_request = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
            result = json.loads(response_body or "{}")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OperationalCommandError(
            f"webhook rejected the event (HTTP {exc.code}): {detail}"
        ) from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise OperationalCommandError(
            f"cannot reach {url}; start the server with 'nexus-seed' first: {exc}"
        ) from exc
    except ValueError as exc:
        raise OperationalCommandError(
            f"webhook returned invalid JSON from {url}"
        ) from exc
    if not isinstance(result, dict):
        raise OperationalCommandError("webhook response must be a JSON object")
    return {
        "submitted": True,
        "event_type": event_type,
        "source_event_key": key,
        **result,
    }


def submit_control_command(
    *,
    host: str,
    port: int,
    token: str | None,
    command: str,
    idempotency_key: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Submit one explicit slash command to the running ConsoleService."""

    url = _webhook_url(host, port).removesuffix("/ingress/webhook") + "/control"
    body = json.dumps(
        {
            "command": command,
            "source_channel": "cli",
            "source_message_id": str(uuid.uuid4()),
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["X-Ingress-Token"] = token
    http_request = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OperationalCommandError(
            f"control endpoint rejected the command (HTTP {exc.code}): {detail}"
        ) from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise OperationalCommandError(
            f"cannot reach {url}; start the server with 'nexus-seed' first: {exc}"
        ) from exc
    except ValueError as exc:
        raise OperationalCommandError("control endpoint returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise OperationalCommandError("control response must be a JSON object")
    return result


def read_status(database_path: Path, *, limit: int = 10) -> dict[str, Any]:
    """Read a compact operational status snapshot from SQLite."""

    path = database_path.resolve()
    if not path.is_file():
        return {
            "status": "not_initialized",
            "initialized": False,
            "database": str(path),
            "hint": "Run 'nexus-seed --once' to initialize the database.",
        }
    with _read_only_connection(path) as connection:
        work_by_status = _group_counts(connection, "work_requirements", "status")
        process_by_status = _group_counts(connection, "process_instances", "status")
        delivery_by_status = _group_counts(connection, "event_deliveries", "status")
        provider_by_status = _group_counts(connection, "provider_invocations", "status")
        reviews = _pending_reviews(connection)
        recent_work = _rows(
            connection,
            """
            SELECT id, work_type, status, priority, reason, updated_at
            FROM work_requirements
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        recent_processes = _rows(
            connection,
            """
            SELECT id, definition_name, definition_version, status,
                   retry_count, last_error, updated_at
            FROM process_instances
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return {
            "status": "ok",
            "initialized": True,
            "database": str(path),
            "counts": {
                "events": _table_count(connection, "events"),
                "continuations": _table_count(connection, "continuations"),
                "pending_reviews": len(reviews),
            },
            "work_by_status": work_by_status,
            "processes_by_status": process_by_status,
            "deliveries_by_status": delivery_by_status,
            "provider_invocations_by_status": provider_by_status,
            "recent_work": recent_work,
            "recent_processes": recent_processes,
        }


def read_pending_reviews(database_path: Path) -> list[PendingReview]:
    """Return all durable human-review Continuations from SQLite."""

    path = database_path.resolve()
    if not path.is_file():
        raise OperationalCommandError(
            f"database does not exist: {path}; run 'nexus-seed --once' first"
        )
    with _read_only_connection(path) as connection:
        return _pending_reviews(connection)


def resolve_pending_review(database_path: Path, review_id: str) -> PendingReview:
    """Resolve an operator-facing id to exactly one pending review."""

    needle = review_id.strip()
    matches = [
        review
        for review in read_pending_reviews(database_path)
        if needle
        in {
            review.review_id,
            review.continuation_id,
            *(str(value) for value in review.match_fields.values()),
        }
    ]
    if not matches:
        raise OperationalCommandError(
            f"no pending review matches {review_id!r}; run 'nexus-seed reviews'"
        )
    if len(matches) > 1:
        ids = ", ".join(review.continuation_id for review in matches)
        raise OperationalCommandError(
            f"review id {review_id!r} is ambiguous ({ids}); use a continuation id"
        )
    return matches[0]


def build_review_payload(
    review: PendingReview,
    decision: str,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a review Event payload without allowing its match key to drift."""

    payload = dict(review.match_fields)
    for key, value in (extra_payload or {}).items():
        if key in payload and payload[key] != value:
            raise OperationalCommandError(
                f"--payload cannot replace review match field {key!r}"
            )
        payload[key] = value
    payload["decision"] = decision.strip().lower()
    if not payload["decision"]:
        raise OperationalCommandError("review decision must not be empty")
    return payload


def _webhook_url(host: str, port: int) -> str:
    client_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in client_host and not client_host.startswith("["):
        client_host = f"[{client_host}]"
    return f"http://{client_host}:{port}/ingress/webhook"


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open and reliably close a SQLite URI connection in read-only mode."""

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def _group_counts(
    connection: sqlite3.Connection, table: str, column: str
) -> dict[str, int]:
    try:
        rows = connection.execute(
            f"SELECT {column} AS value, COUNT(*) AS count "
            f"FROM {table} GROUP BY {column} ORDER BY {column}"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    return {str(row["value"]): int(row["count"]) for row in rows}


def _rows(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def _pending_reviews(connection: sqlite3.Connection) -> list[PendingReview]:
    rows = connection.execute(
        """
        SELECT c.id AS continuation_id, c.process_instance_id, c.resume_point,
               c.waiting_for, c.created_at, p.definition_name
        FROM continuations AS c
        JOIN process_instances AS p ON p.id = c.process_instance_id
        ORDER BY c.created_at ASC, c.id ASC
        """
    ).fetchall()
    reviews: list[PendingReview] = []
    for row in rows:
        try:
            waiting_for = json.loads(row["waiting_for"])
        except (TypeError, ValueError):
            continue
        for condition in _flat_conditions(waiting_for):
            event_type = str(condition.get("event_type") or "")
            if not event_type.endswith("_reviewed"):
                continue
            match_fields = {
                str(key): value
                for key, value in condition.items()
                if key != "event_type"
            }
            review_id = _review_id(match_fields, str(row["continuation_id"]))
            reviews.append(
                PendingReview(
                    continuation_id=str(row["continuation_id"]),
                    process_instance_id=str(row["process_instance_id"]),
                    process_definition=str(row["definition_name"]),
                    resume_point=str(row["resume_point"]),
                    event_type=event_type,
                    match_fields=match_fields,
                    review_id=review_id,
                    created_at=str(row["created_at"]),
                )
            )
    return reviews


def _flat_conditions(waiting_for: object) -> list[dict[str, Any]]:
    if not isinstance(waiting_for, dict):
        return []
    alternatives = waiting_for.get("any")
    if isinstance(alternatives, list):
        flattened: list[dict[str, Any]] = []
        for alternative in alternatives:
            flattened.extend(_flat_conditions(alternative))
        return flattened
    return [waiting_for]


def _review_id(match_fields: dict[str, Any], fallback: str) -> str:
    preferred = (
        "proposal_id",
        "installation_plan_id",
        "acquisition_session_id",
        "plan_id",
    )
    for key in preferred:
        if key in match_fields:
            return str(match_fields[key])
    for key, value in match_fields.items():
        if key.endswith("_id"):
            return str(value)
    return fallback
