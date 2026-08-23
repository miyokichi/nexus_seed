"""Operational helpers for the human-facing NEXUS SEED CLI.

Write commands go through the webhook ingress boundary.  Inspection commands
open SQLite in read-only mode, so they can safely be used while the application
server owns the normal read/write connection.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from urllib import error, request
import uuid


class OperationalCommandError(RuntimeError):
    """Raised when an operational CLI command cannot be completed."""


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


def read_status(database_path: Path, *, limit: int = 10) -> dict[str, Any]:
    """Read a compact Project/Knowledge operational snapshot from SQLite."""

    path = database_path.resolve()
    if not path.is_file():
        return {
            "status": "not_initialized",
            "initialized": False,
            "database": str(path),
            "hint": "Run 'nexus-seed --once' to initialize the database.",
        }
    with _read_only_connection(path) as connection:
        project_by_status = _group_counts(connection, "orchestrator_projects", "status")
        agent_by_status = _group_counts(connection, "orchestrator_agents", "status")
        delivery_by_status = _group_counts(connection, "event_deliveries", "status")
        knowledge_by_kind = _group_counts(connection, "knowledge_revisions", "kind")
        pending_reviews = connection.execute(
            "SELECT COUNT(*) AS count FROM knowledge_revisions WHERE status = ?",
            ("PENDING_REVIEW",),
        ).fetchone()
        recent_projects = _rows(
            connection,
            """
            SELECT id, goal, status, priority, assigned_agent_id, summary, updated_at
            FROM orchestrator_projects
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        recent_agents = _rows(
            connection,
            """
            SELECT agent_id, project_id, runtime, status, endpoint, updated_at
            FROM orchestrator_agents
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
                "projects": _table_count(connection, "orchestrator_projects"),
                "agents": _table_count(connection, "orchestrator_agents"),
                "knowledge_revisions": _table_count(connection, "knowledge_revisions"),
                "pending_reviews": int(pending_reviews["count"]),
            },
            "projects_by_status": project_by_status,
            "agents_by_status": agent_by_status,
            "deliveries_by_status": delivery_by_status,
            "knowledge_by_kind": knowledge_by_kind,
            "recent_projects": recent_projects,
            "recent_agents": recent_agents,
        }


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
