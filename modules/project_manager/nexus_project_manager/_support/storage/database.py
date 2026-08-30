"""SQLite database wrapper and the active NEXUS SEED schema.

Persistence uses the standard-library :mod:`sqlite3`; JSON-shaped values are
stored in ``TEXT`` columns. Opening an existing database is deliberately
non-destructive: retired tables and columns are left in place as historical
data, while new databases receive only the active Project/Knowledge schema.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT,
    causation_id TEXT,
    ingress_receipt_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id);

CREATE TABLE IF NOT EXISTS event_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_delivery_dispatch
    ON event_deliveries(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS process_definitions (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    handler TEXT NOT NULL,
    trigger_event_types TEXT NOT NULL DEFAULT '[]',
    max_retries INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    context_requirements TEXT,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS process_instances (
    id TEXT PRIMARY KEY,
    definition_name TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input TEXT NOT NULL,
    local_state TEXT NOT NULL,
    parent_process_id TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    pending_event_id TEXT,
    trigger_event_id TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_status ON process_instances(status);
CREATE INDEX IF NOT EXISTS idx_instances_parent ON process_instances(parent_process_id);
CREATE INDEX IF NOT EXISTS idx_instances_trigger ON process_instances(trigger_event_id);

CREATE TABLE IF NOT EXISTS continuations (
    id TEXT PRIMARY KEY,
    process_instance_id TEXT NOT NULL,
    resume_point TEXT NOT NULL,
    waiting_for TEXT NOT NULL,
    saved_process_state TEXT NOT NULL,
    context_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cont_instance ON continuations(process_instance_id);

CREATE TABLE IF NOT EXISTS world_state_history (
    id TEXT PRIMARY KEY,
    entity TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    version INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_event TEXT,
    observation_id TEXT,
    state_delta_id TEXT,
    created_by_process_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_key
    ON world_state_history(entity, attribute, version);

CREATE TABLE IF NOT EXISTS world_state_current (
    entity TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    version INTEGER NOT NULL,
    history_id TEXT,
    source_event TEXT,
    observation_id TEXT,
    state_delta_id TEXT,
    created_by_process_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entity, attribute)
);

CREATE TABLE IF NOT EXISTS timers (
    id TEXT PRIMARY KEY,
    fire_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timers_due ON timers(fired, fire_at);

CREATE TABLE IF NOT EXISTS joins (
    id TEXT PRIMARY KEY,
    parent_instance_id TEXT NOT NULL,
    child_ids TEXT NOT NULL,
    mode TEXT NOT NULL,
    resume_point TEXT NOT NULL,
    saved_process_state TEXT NOT NULL,
    completed TEXT NOT NULL DEFAULT '[]',
    satisfied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_activations (
    activation_key TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_snapshots (
    id TEXT PRIMARY KEY,
    process_instance_id TEXT NOT NULL,
    activation_id TEXT,
    trigger_event_id TEXT,
    context_json TEXT NOT NULL,
    compiled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ctxsnap_instance
    ON context_snapshots(process_instance_id);

CREATE TABLE IF NOT EXISTS ingress_receipts (
    id TEXT PRIMARY KEY,
    adapter_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_cursor TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    event_id TEXT,
    status TEXT NOT NULL,
    UNIQUE (adapter_id, source_event_key)
);
CREATE INDEX IF NOT EXISTS idx_ingress_event ON ingress_receipts(event_id);
CREATE INDEX IF NOT EXISTS idx_ingress_adapter
    ON ingress_receipts(adapter_id, status);

CREATE TABLE IF NOT EXISTS adapter_checkpoints (
    adapter_id TEXT NOT NULL,
    stream_key TEXT NOT NULL,
    cursor TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (adapter_id, stream_key)
);

CREATE TABLE IF NOT EXISTS observation_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '[]',
    poll_interval_seconds REAL NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    last_checked_at TEXT,
    last_changed_at TEXT,
    last_event_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observation_sources_enabled
    ON observation_sources(enabled, kind);

CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    uri TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL DEFAULT 'unknown',
    source_adapter_id TEXT,
    source_identity TEXT,
    current_version_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_versions (
    id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT,
    size_bytes INTEGER,
    source_event_id TEXT,
    ingress_receipt_id TEXT,
    locator TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (resource_id, version)
);
CREATE INDEX IF NOT EXISTS idx_rversion_resource
    ON resource_versions(resource_id, version);
CREATE INDEX IF NOT EXISTS idx_rversion_hash
    ON resource_versions(resource_id, content_hash);

CREATE TABLE IF NOT EXISTS resource_representations (
    id TEXT PRIMARY KEY,
    resource_version_id TEXT NOT NULL,
    representation_type TEXT NOT NULL,
    content_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_by_process_id TEXT,
    extractor_name TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT '1',
    created_at TEXT NOT NULL,
    UNIQUE (
        resource_version_id,
        representation_type,
        extractor_name,
        extractor_version
    )
);
CREATE INDEX IF NOT EXISTS idx_repr_version
    ON resource_representations(resource_version_id);

CREATE TABLE IF NOT EXISTS project_chat_threads (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_chat_messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    thread_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    certainty TEXT,
    references_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_chat_message_thread
    ON project_chat_messages(thread_id, seq);

CREATE TABLE IF NOT EXISTS orchestrator_projects (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    assigned_agent_id TEXT,
    parent_project_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    blockers TEXT NOT NULL DEFAULT '[]',
    tasks TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_projects_status
    ON orchestrator_projects(status);
CREATE INDEX IF NOT EXISTS idx_orch_projects_parent
    ON orchestrator_projects(parent_project_id);

CREATE TABLE IF NOT EXISTS orchestrator_agents (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    runtime TEXT NOT NULL,
    status TEXT NOT NULL,
    endpoint TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_agents_project
    ON orchestrator_agents(project_id);
CREATE INDEX IF NOT EXISTS idx_orch_agents_status
    ON orchestrator_agents(status);

CREATE TABLE IF NOT EXISTS orchestrator_a2a_messages (
    id TEXT PRIMARY KEY,
    source_agent_id TEXT,
    project_id TEXT,
    direction TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_a2a_project
    ON orchestrator_a2a_messages(project_id);

CREATE TABLE IF NOT EXISTS orchestrator_instructions (
    instruction_key TEXT PRIMARY KEY,
    origin_project_id TEXT,
    request_id TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT '{}',
    affected_project_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_instr_project
    ON orchestrator_instructions(origin_project_id);

CREATE TABLE IF NOT EXISTS knowledge_revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    knowledge_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_format TEXT NOT NULL,
    content_value TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    recorded_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    parents TEXT NOT NULL DEFAULT '[]',
    relations TEXT NOT NULL DEFAULT '[]',
    annotations TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL DEFAULT 'raw',
    status TEXT,
    derived_from TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (knowledge_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_id
    ON knowledge_revisions(knowledge_id, revision);
CREATE INDEX IF NOT EXISTS idx_knowledge_kind ON knowledge_revisions(kind);
CREATE INDEX IF NOT EXISTS idx_knowledge_status ON knowledge_revisions(status);
CREATE INDEX IF NOT EXISTS idx_knowledge_source
    ON knowledge_revisions(source_type, source_ref);
CREATE INDEX IF NOT EXISTS idx_knowledge_recorded_at
    ON knowledge_revisions(knowledge_id, recorded_at);
"""


# ``CREATE TABLE IF NOT EXISTS`` cannot widen an existing table. These two
# additions are required for databases created before ingress and durable
# delivery shipped. Retired tables are preserved but no longer migrated.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("events", "ingress_receipt_id", "TEXT"),
    ("process_instances", "trigger_event_id", "TEXT"),
)


def dumps(value: Any) -> str:
    """Serialize a value to compact JSON for a ``TEXT`` column."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(text: str | None) -> Any:
    """Deserialize a JSON ``TEXT`` value, treating ``NULL`` as ``None``."""
    if text is None:
        return None
    return json.loads(text)


class Database:
    """Own a single SQLite connection and the active schema."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self.init_schema()

    def init_schema(self) -> None:
        """Create active tables and non-destructively widen old databases."""
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        """Apply the small, idempotent compatibility migration set."""
        for table, column, ddl in ADDED_COLUMNS:
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a statement, committing unless inside :meth:`atomic`."""
        cursor = self.conn.execute(sql, params)
        if self._depth == 0:
            self.conn.commit()
        return cursor

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Commit all writes in the block together, or roll them back."""
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
        """Execute a query and return its first row, if any."""
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()
