"""SQLite database wrapper and schema for NEXUS SEED Phase 1.

Persistence deliberately uses the standard-library :mod:`sqlite3` — no ORM, no
external dependency.  JSON-shaped values are stored as ``TEXT`` columns.  The
schema is the only thing that must survive a runtime restart; rebuilding a
:class:`~nexus_seed.runtime.runtime.Runtime` from the same database file must
fully restore process state.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT UNIQUE NOT NULL,
    type           TEXT NOT NULL,
    source         TEXT NOT NULL,
    payload        TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    correlation_id TEXT,
    causation_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id);

CREATE TABLE IF NOT EXISTS process_definitions (
    name                TEXT NOT NULL,
    version             TEXT NOT NULL,
    handler             TEXT NOT NULL,
    trigger_event_types TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS process_instances (
    id                 TEXT PRIMARY KEY,
    definition_name    TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    status             TEXT NOT NULL,
    input              TEXT NOT NULL,
    local_state        TEXT NOT NULL,
    parent_process_id  TEXT,
    priority           INTEGER NOT NULL DEFAULT 0,
    pending_event_id   TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_status ON process_instances(status);

CREATE TABLE IF NOT EXISTS continuations (
    id                  TEXT PRIMARY KEY,
    process_instance_id TEXT NOT NULL,
    resume_point        TEXT NOT NULL,
    waiting_for         TEXT NOT NULL,
    saved_process_state TEXT NOT NULL,
    context_ref         TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cont_instance ON continuations(process_instance_id);

CREATE TABLE IF NOT EXISTS world_state (
    entity       TEXT NOT NULL,
    attribute    TEXT NOT NULL,
    value        TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    source_event TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (entity, attribute)
);
"""


def dumps(value: Any) -> str:
    """Serialize a value to a compact JSON string for a TEXT column."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(text: str | None) -> Any:
    """Deserialize a JSON TEXT column value, treating NULL as ``None``."""
    if text is None:
        return None
    return json.loads(text)


class Database:
    """A thin owner of a single SQLite connection plus the schema.

    The connection is used single-threaded from the asyncio event loop, so the
    default sqlite3 threading rules are fine.  Writes commit immediately.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        """Create all tables if they do not already exist."""
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a statement and commit it."""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a query and return all rows."""
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """Execute a query and return the first row (or ``None``)."""
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()
