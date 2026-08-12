"""SQLite database wrapper and schema for NEXUS SEED.

Persistence uses the standard-library :mod:`sqlite3` — no ORM, no external
dependency.  JSON-shaped values are stored as ``TEXT`` columns.

Phase 2A adds :meth:`Database.atomic`: writes made through :meth:`execute`
inside an ``atomic()`` block are deferred and committed together (or rolled
back on error).  Stores need no changes for this — they call :meth:`execute`
as before, and ``atomic()`` decides when the commit happens.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
    max_retries         INTEGER NOT NULL DEFAULT 0,
    metadata            TEXT NOT NULL DEFAULT '{}',
    context_requirements TEXT,
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
    work_key           TEXT,
    work_requirement_id TEXT,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    max_retries        INTEGER NOT NULL DEFAULT 0,
    next_retry_at      TEXT,
    last_error         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_status ON process_instances(status);
CREATE INDEX IF NOT EXISTS idx_instances_work_key ON process_instances(work_key);
CREATE INDEX IF NOT EXISTS idx_instances_work_req ON process_instances(work_requirement_id);

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

CREATE TABLE IF NOT EXISTS world_state_history (
    id                    TEXT PRIMARY KEY,
    entity                TEXT NOT NULL,
    attribute             TEXT NOT NULL,
    value                 TEXT NOT NULL,
    version               INTEGER NOT NULL,
    valid_from            TEXT NOT NULL,
    valid_to              TEXT,
    source_event          TEXT,
    observation_id        TEXT,
    state_delta_id        TEXT,
    created_by_process_id TEXT,
    confidence            REAL NOT NULL DEFAULT 1.0,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_key ON world_state_history(entity, attribute, version);

CREATE TABLE IF NOT EXISTS world_state_current (
    entity                TEXT NOT NULL,
    attribute             TEXT NOT NULL,
    value                 TEXT NOT NULL,
    version               INTEGER NOT NULL,
    history_id            TEXT,
    source_event          TEXT,
    observation_id        TEXT,
    state_delta_id        TEXT,
    created_by_process_id TEXT,
    confidence            REAL NOT NULL DEFAULT 1.0,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (entity, attribute)
);

CREATE TABLE IF NOT EXISTS observations (
    id                    TEXT PRIMARY KEY,
    source_event_id       TEXT,
    created_by_process_id TEXT,
    subject               TEXT NOT NULL,
    predicate             TEXT NOT NULL,
    extracted             TEXT NOT NULL,
    confidence            REAL NOT NULL DEFAULT 1.0,
    proposal_id           TEXT,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_event ON observations(source_event_id);

CREATE TABLE IF NOT EXISTS state_deltas (
    id                    TEXT PRIMARY KEY,
    entity                TEXT NOT NULL,
    attribute             TEXT NOT NULL,
    old_value             TEXT,
    new_value             TEXT,
    source_event_id       TEXT,
    observation_id        TEXT,
    created_by_process_id TEXT,
    confidence            REAL NOT NULL DEFAULT 1.0,
    reason                TEXT,
    valid_from            TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delta_key ON state_deltas(entity, attribute);

CREATE TABLE IF NOT EXISTS timers (
    id            TEXT PRIMARY KEY,
    fire_at       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    fired         INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timers_due ON timers(fired, fire_at);

CREATE TABLE IF NOT EXISTS joins (
    id                  TEXT PRIMARY KEY,
    parent_instance_id  TEXT NOT NULL,
    child_ids           TEXT NOT NULL,
    mode                TEXT NOT NULL,
    resume_point        TEXT NOT NULL,
    saved_process_state TEXT NOT NULL,
    completed           TEXT NOT NULL DEFAULT '[]',
    satisfied           INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_activations (
    activation_key TEXT PRIMARY KEY,
    instance_id    TEXT NOT NULL,
    event_id       TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_requirements (
    id                    TEXT PRIMARY KEY,
    work_type             TEXT NOT NULL,
    work_key              TEXT NOT NULL UNIQUE,
    related_entities      TEXT NOT NULL DEFAULT '[]',
    reason                TEXT,
    source_event_id       TEXT,
    source_state_delta_id TEXT,
    priority              INTEGER NOT NULL DEFAULT 0,
    status                TEXT NOT NULL,
    metadata              TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_status ON work_requirements(status);

CREATE TABLE IF NOT EXISTS context_snapshots (
    id                  TEXT PRIMARY KEY,
    process_instance_id TEXT NOT NULL,
    activation_id       TEXT,
    trigger_event_id    TEXT,
    context_json        TEXT NOT NULL,
    compiled_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ctxsnap_instance ON context_snapshots(process_instance_id);

CREATE TABLE IF NOT EXISTS interpretation_proposals (
    id                    TEXT PRIMARY KEY,
    source_event_id       TEXT,
    created_by_process_id TEXT,
    context_snapshot_id   TEXT,
    llm_invocation_id     TEXT,
    proposal_json         TEXT NOT NULL,
    confidence            REAL NOT NULL DEFAULT 0.0,
    decision              TEXT NOT NULL DEFAULT 'PENDING',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposal_decision ON interpretation_proposals(decision);

CREATE TABLE IF NOT EXISTS llm_invocations (
    id                  TEXT PRIMARY KEY,
    process_instance_id TEXT NOT NULL,
    activation_id       TEXT,
    backend             TEXT NOT NULL,
    model               TEXT,
    request_metadata    TEXT NOT NULL DEFAULT '{}',
    response_metadata   TEXT NOT NULL DEFAULT '{}',
    context_snapshot_id TEXT,
    success             INTEGER NOT NULL DEFAULT 1,
    error               TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llminv_instance ON llm_invocations(process_instance_id);
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
    """Owns a single SQLite connection plus the schema.

    The connection is used single-threaded from the asyncio event loop, so the
    default sqlite3 threading rules are fine.  A standalone :meth:`execute`
    commits immediately; inside an :meth:`atomic` block commits are deferred so
    a whole process activation lands (or rolls back) as one transaction.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self.init_schema()

    def init_schema(self) -> None:
        """Create all tables if they do not already exist."""
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a statement, committing immediately unless inside ``atomic``."""
        cur = self.conn.execute(sql, params)
        if self._depth == 0:
            self.conn.commit()
        return cur

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Group all :meth:`execute` writes in the block into one transaction.

        Commits on clean exit, rolls back on exception.  Not reentrant across
        independent activations, but nesting is tolerated (only the outermost
        block commits).
        """
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a query and return all rows."""
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """Execute a query and return the first row (or ``None``)."""
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()
