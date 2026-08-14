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
    causation_id   TEXT,
    -- Phase 3D: set only on events that entered from outside, so
    -- "did this come from the world or from us?" is answerable directly.
    ingress_receipt_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id);

CREATE TABLE IF NOT EXISTS event_deliveries (
    id              TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    delivered_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_delivery_dispatch
    ON event_deliveries(status, next_attempt_at);

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
    trigger_event_id   TEXT,
    plan_id            TEXT,
    plan_node_id       TEXT,
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
    required_capabilities_json  TEXT,
    missing_capabilities_json   TEXT,
    selected_definition_name    TEXT,
    selected_definition_version TEXT,
    available_input_types_json  TEXT,
    required_output_types_json  TEXT,
    selected_plan_id            TEXT,
    -- Phase 4C: what this need is optimising for, and how many times it may
    -- be replanned before the system stops trying (never CANCELs it).
    decision_preference_json    TEXT,
    max_replans                 INTEGER,
    replan_count                INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS action_proposals (
    id                        TEXT PRIMARY KEY,
    created_by_process_id     TEXT,
    source_work_requirement_id TEXT,
    trigger_event_id          TEXT,
    context_snapshot_id       TEXT,
    backend                   TEXT NOT NULL,
    action_type               TEXT NOT NULL,
    target                    TEXT,
    parameters_json           TEXT NOT NULL DEFAULT '{}',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    declared_side_effects_json TEXT NOT NULL DEFAULT '[]',
    risk_level                TEXT NOT NULL,
    status                    TEXT NOT NULL,
    rationale                 TEXT,
    idempotency_key           TEXT,
    root_proposal_id          TEXT,
    replaces_proposal_id      TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_proposal_status ON action_proposals(status);
CREATE INDEX IF NOT EXISTS idx_action_proposal_root ON action_proposals(root_proposal_id);

CREATE TABLE IF NOT EXISTS action_executions (
    id                  TEXT PRIMARY KEY,
    action_proposal_id  TEXT NOT NULL,
    process_instance_id TEXT NOT NULL,
    backend             TEXT NOT NULL,
    action_type         TEXT NOT NULL,
    status              TEXT NOT NULL,
    attempt             INTEGER NOT NULL DEFAULT 1,
    idempotency_key     TEXT,
    result_json         TEXT,
    error               TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_exec_proposal ON action_executions(action_proposal_id);
CREATE INDEX IF NOT EXISTS idx_action_exec_idem ON action_executions(idempotency_key, status);

CREATE TABLE IF NOT EXISTS action_decisions (
    id                        TEXT PRIMARY KEY,
    action_proposal_id        TEXT NOT NULL,
    decision                  TEXT NOT NULL,
    risk_level                TEXT NOT NULL,
    decided_by_process_id     TEXT,
    process_definition_name   TEXT,
    process_definition_version TEXT,
    granted_permissions_json  TEXT NOT NULL DEFAULT '[]',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    mandatory_permissions_json TEXT NOT NULL DEFAULT '[]',
    reasons_json              TEXT NOT NULL DEFAULT '[]',
    policy_json               TEXT NOT NULL DEFAULT '{}',
    reviewed_by_event_id      TEXT,
    created_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_decision_proposal ON action_decisions(action_proposal_id);

CREATE TABLE IF NOT EXISTS ingress_receipts (
    id               TEXT PRIMARY KEY,
    adapter_id       TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    source_event_key TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    source_cursor    TEXT,
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    reasons_json     TEXT NOT NULL DEFAULT '[]',
    observed_at      TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    event_id         TEXT,
    status           TEXT NOT NULL,
    UNIQUE (adapter_id, source_event_key)
);
CREATE INDEX IF NOT EXISTS idx_ingress_event ON ingress_receipts(event_id);
CREATE INDEX IF NOT EXISTS idx_ingress_adapter ON ingress_receipts(adapter_id, status);

CREATE TABLE IF NOT EXISTS capabilities (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    version          TEXT NOT NULL,
    description      TEXT,
    input_types_json TEXT NOT NULL DEFAULT '[]',
    output_types_json TEXT NOT NULL DEFAULT '[]',
    tags_json        TEXT NOT NULL DEFAULT '[]',
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (name, version)
);
CREATE INDEX IF NOT EXISTS idx_capability_name ON capabilities(name, enabled);

CREATE TABLE IF NOT EXISTS process_capabilities (
    id                 TEXT PRIMARY KEY,
    definition_name    TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    capability_id      TEXT NOT NULL,
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    UNIQUE (definition_name, definition_version, capability_id)
);
CREATE INDEX IF NOT EXISTS idx_proccap_capability ON process_capabilities(capability_id);

CREATE TABLE IF NOT EXISTS capability_work_matches (
    id                         TEXT PRIMARY KEY,
    work_requirement_id        TEXT NOT NULL,
    status                     TEXT NOT NULL,
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    candidates_json            TEXT NOT NULL DEFAULT '[]',
    missing_capabilities_json  TEXT NOT NULL DEFAULT '[]',
    selected_definition_name   TEXT,
    selected_definition_version TEXT,
    reasons_json               TEXT NOT NULL DEFAULT '[]',
    created_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capmatch_work ON capability_work_matches(work_requirement_id);

CREATE TABLE IF NOT EXISTS process_plans (
    id                        TEXT PRIMARY KEY,
    work_requirement_id       TEXT NOT NULL,
    status                    TEXT NOT NULL,
    planner_name              TEXT NOT NULL DEFAULT 'composition',
    planner_version           TEXT NOT NULL DEFAULT '1',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    input_types_json          TEXT NOT NULL DEFAULT '[]',
    required_output_types_json TEXT NOT NULL DEFAULT '[]',
    planning_snapshot_json    TEXT NOT NULL DEFAULT '{}',
    reasons_json              TEXT NOT NULL DEFAULT '[]',
    created_by_process_id     TEXT,
    -- Phase 4C: the identity of this plan's *shape*, so candidates can be
    -- compared and a failed shape excluded from a later attempt.
    fingerprint               TEXT,
    replan_attempt            INTEGER NOT NULL DEFAULT 0,
    supersedes_plan_id        TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    completed_at              TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_work ON process_plans(work_requirement_id, status);
CREATE INDEX IF NOT EXISTS idx_plan_fingerprint
    ON process_plans(work_requirement_id, fingerprint);

CREATE TABLE IF NOT EXISTS plan_nodes (
    id                  TEXT PRIMARY KEY,
    plan_id             TEXT NOT NULL,
    node_key            TEXT NOT NULL,
    definition_name     TEXT NOT NULL,
    definition_version  TEXT NOT NULL,
    provided_capabilities_json TEXT NOT NULL DEFAULT '[]',
    input_types_json    TEXT NOT NULL DEFAULT '[]',
    output_types_json   TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL,
    process_instance_id TEXT,
    depth               INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    -- One logical position per plan: what makes node execution idempotent.
    UNIQUE (plan_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_plannode_plan ON plan_nodes(plan_id, status);

CREATE TABLE IF NOT EXISTS plan_edges (
    id            TEXT PRIMARY KEY,
    plan_id       TEXT NOT NULL,
    from_node_id  TEXT NOT NULL,
    to_node_id    TEXT NOT NULL,
    artifact_type TEXT,
    output_type   TEXT,
    input_type    TEXT,
    output_key    TEXT,
    input_key     TEXT,
    created_at    TEXT NOT NULL
);
-- One producer per consumer input (Invariant 67).  A partial index because
-- SQLite treats NULLs as distinct, which would let legacy edges collide.
CREATE UNIQUE INDEX IF NOT EXISTS idx_planedge_one_producer_per_input
    ON plan_edges(plan_id, to_node_id, input_type, IFNULL(input_key, ''))
    WHERE input_type IS NOT NULL;
-- The Phase 4B rule, kept only for edges that carry no binding: one dependency
-- of a given type between two nodes.  It cannot apply to bound edges, because
-- one node legitimately feeds two keyed inputs of another (spec §14).
CREATE UNIQUE INDEX IF NOT EXISTS idx_planedge_legacy_dependency
    ON plan_edges(plan_id, from_node_id, to_node_id, artifact_type)
    WHERE input_type IS NULL;
CREATE INDEX IF NOT EXISTS idx_planedge_plan ON plan_edges(plan_id);

-- Phase 4C: what each candidate plan is estimated to cost, take, risk and
-- yield.  Append-only (Invariant 78) — a decision history that can be
-- overwritten explains nothing afterwards.
CREATE TABLE IF NOT EXISTS plan_evaluations (
    id                      TEXT PRIMARY KEY,
    plan_id                 TEXT NOT NULL,
    work_requirement_id     TEXT,
    fingerprint             TEXT,
    estimated_cost          REAL,
    estimated_latency       REAL,
    estimated_risk          REAL,
    estimated_quality       REAL,
    estimated_reliability   REAL,
    node_count              INTEGER NOT NULL DEFAULT 0,
    depth                   INTEGER NOT NULL DEFAULT 0,
    human_approval_required INTEGER NOT NULL DEFAULT 0,
    reasons_json            TEXT NOT NULL DEFAULT '[]',
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL,
    -- One evaluation per plan: the same plan evaluated twice by the same
    -- deterministic rules is the same evaluation (spec §103).
    UNIQUE (plan_id)
);
CREATE INDEX IF NOT EXISTS idx_planeval_work
    ON plan_evaluations(work_requirement_id, created_at);

-- An LLM's suggestion about which candidate to run.  Never the decision.
CREATE TABLE IF NOT EXISTS plan_selection_proposals (
    id                      TEXT PRIMARY KEY,
    work_requirement_id     TEXT NOT NULL,
    candidate_plan_ids_json TEXT NOT NULL DEFAULT '[]',
    selected_plan_id        TEXT,
    confidence              REAL NOT NULL DEFAULT 0,
    rationale               TEXT,
    status                  TEXT NOT NULL,
    reasons_json            TEXT NOT NULL DEFAULT '[]',
    context_snapshot_id     TEXT,
    llm_invocation_id       TEXT,
    created_by_process_id   TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selproposal_work
    ON plan_selection_proposals(work_requirement_id, created_at);

-- What the system actually committed to, and how it got there.
CREATE TABLE IF NOT EXISTS plan_selections (
    id                     TEXT PRIMARY KEY,
    work_requirement_id    TEXT NOT NULL,
    selected_plan_id       TEXT,
    selection_method       TEXT NOT NULL,
    deterministic_score    REAL,
    selection_proposal_id  TEXT,
    replan_attempt         INTEGER NOT NULL DEFAULT 0,
    considered_plan_ids_json TEXT NOT NULL DEFAULT '[]',
    rejected_plan_ids_json TEXT NOT NULL DEFAULT '[]',
    decision_reasons_json  TEXT NOT NULL DEFAULT '[]',
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_planselection_work
    ON plan_selections(work_requirement_id, created_at);

-- One attempt to find another way after a plan failed terminally.  The UNIQUE
-- key is the logical identity of an attempt (spec §93), which is what makes a
-- redelivered replan_required find the attempt already made instead of making
-- a second one.
CREATE TABLE IF NOT EXISTS replan_attempts (
    id                        TEXT PRIMARY KEY,
    work_requirement_id       TEXT NOT NULL,
    previous_plan_id          TEXT,
    attempt_number            INTEGER NOT NULL DEFAULT 1,
    excluded_fingerprints_json TEXT NOT NULL DEFAULT '[]',
    excluded_definitions_json TEXT NOT NULL DEFAULT '[]',
    candidate_plan_ids_json   TEXT NOT NULL DEFAULT '[]',
    selected_plan_id          TEXT,
    failure_reason            TEXT,
    reasons_json              TEXT NOT NULL DEFAULT '[]',
    created_at                TEXT NOT NULL,
    UNIQUE (work_requirement_id, previous_plan_id)
);
CREATE INDEX IF NOT EXISTS idx_replan_work
    ON replan_attempts(work_requirement_id, created_at);

-- Phase 5A: what the system cannot do, as distinct from what the world needs.
-- The UNIQUE key is the *logical* identity of a deficiency (spec §127), so a
-- redelivered capability_missing finds the gap it already opened.
CREATE TABLE IF NOT EXISTS capability_gaps (
    id                             TEXT PRIMARY KEY,
    work_requirement_id            TEXT NOT NULL,
    missing_key                    TEXT NOT NULL,
    required_capabilities_json     TEXT NOT NULL DEFAULT '[]',
    missing_capabilities_json      TEXT NOT NULL DEFAULT '[]',
    current_partial_providers_json TEXT NOT NULL DEFAULT '[]',
    reason                         TEXT,
    source_match_id                TEXT,
    status                         TEXT NOT NULL,
    created_at                     TEXT NOT NULL,
    updated_at                     TEXT NOT NULL,
    UNIQUE (work_requirement_id, missing_key)
);
CREATE INDEX IF NOT EXISTS idx_capgap_status ON capability_gaps(status);
CREATE INDEX IF NOT EXISTS idx_capgap_work ON capability_gaps(work_requirement_id);

-- How a gap might be closed.  A description, never an applied change
-- (Invariant 90).  UNIQUE on the content fingerprint so a re-run of the same
-- analysis is the same proposal rather than a second one (spec §68).
CREATE TABLE IF NOT EXISTS extension_proposals (
    id                        TEXT PRIMARY KEY,
    capability_gap_id         TEXT NOT NULL,
    work_requirement_id       TEXT,
    fingerprint               TEXT NOT NULL UNIQUE,
    strategy                  TEXT,
    declared_strategy         TEXT,
    title                     TEXT NOT NULL DEFAULT '',
    description               TEXT NOT NULL DEFAULT '',
    target_capabilities_json  TEXT NOT NULL DEFAULT '[]',
    proposed_components_json  TEXT NOT NULL DEFAULT '[]',
    reusable_components_json  TEXT NOT NULL DEFAULT '[]',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    candidate_strategies_json TEXT NOT NULL DEFAULT '[]',
    analysis_json             TEXT NOT NULL DEFAULT '{}',
    estimated_risk            TEXT NOT NULL,
    estimated_cost            REAL,
    feasibility               TEXT NOT NULL DEFAULT 'UNKNOWN',
    human_approval_required   INTEGER NOT NULL DEFAULT 1,
    rationale                 TEXT,
    source                    TEXT NOT NULL DEFAULT 'deterministic',
    status                    TEXT NOT NULL,
    reasons_json              TEXT NOT NULL DEFAULT '[]',
    context_snapshot_id       TEXT,
    llm_invocation_id         TEXT,
    created_by_process_id     TEXT,
    root_proposal_id          TEXT,
    replaces_proposal_id      TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extprop_gap
    ON extension_proposals(capability_gap_id, created_at);
CREATE INDEX IF NOT EXISTS idx_extprop_status ON extension_proposals(status);
CREATE INDEX IF NOT EXISTS idx_extprop_root ON extension_proposals(root_proposal_id);

-- Why an extension proposal was approved, reviewed or refused.  Append-only
-- (Invariant 92): a proposal reviewed twice keeps both records.
CREATE TABLE IF NOT EXISTS extension_decisions (
    id                        TEXT PRIMARY KEY,
    extension_proposal_id     TEXT NOT NULL,
    capability_gap_id         TEXT,
    decision                  TEXT NOT NULL,
    estimated_risk            TEXT NOT NULL,
    decided_by_process_id     TEXT,
    validation_ok             INTEGER NOT NULL DEFAULT 1,
    reasons_json              TEXT NOT NULL DEFAULT '[]',
    policy_json               TEXT NOT NULL DEFAULT '{}',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    granted_permissions_json  TEXT NOT NULL DEFAULT '[]',
    reviewed_by_event_id      TEXT,
    created_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extdec_proposal
    ON extension_decisions(extension_proposal_id, created_at);

-- Phase 5D: the durable, bounded coordinator above 5A/5B/5C.  A session is
-- shareable by logical acquisition_key; it never replaces the records owned by
-- the individual phases.
CREATE TABLE IF NOT EXISTS capability_acquisition_sessions (
    id                         TEXT PRIMARY KEY,
    capability_gap_id          TEXT NOT NULL,
    source_work_requirement_id TEXT NOT NULL,
    acquisition_key            TEXT NOT NULL UNIQUE,
    target_capabilities_json   TEXT NOT NULL DEFAULT '[]',
    status                     TEXT NOT NULL,
    current_stage              TEXT NOT NULL,
    extension_proposal_id      TEXT,
    construction_plan_id       TEXT,
    construction_result_id     TEXT,
    installation_plan_id       TEXT,
    parent_session_id          TEXT,
    extension_depth            INTEGER NOT NULL DEFAULT 0,
    construction_attempts      INTEGER NOT NULL DEFAULT 0,
    installation_attempts      INTEGER NOT NULL DEFAULT 0,
    autonomy_budget_json       TEXT NOT NULL DEFAULT '{}',
    blocked_reason             TEXT,
    review_summary_json        TEXT NOT NULL DEFAULT '{}',
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    completed_at               TEXT
);
CREATE INDEX IF NOT EXISTS idx_acquisition_session_status
    ON capability_acquisition_sessions(status, current_stage);
CREATE INDEX IF NOT EXISTS idx_acquisition_session_parent
    ON capability_acquisition_sessions(parent_session_id);

CREATE TABLE IF NOT EXISTS acquisition_subscribers (
    id                     TEXT PRIMARY KEY,
    acquisition_session_id TEXT NOT NULL,
    work_requirement_id    TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE(acquisition_session_id, work_requirement_id)
);
CREATE INDEX IF NOT EXISTS idx_acquisition_subscriber_work
    ON acquisition_subscribers(work_requirement_id, status);

-- Append-only.  decision_key supplies activation idempotency without turning
-- a later-stage decision into an overwrite of an earlier explanation.
CREATE TABLE IF NOT EXISTS autonomy_decisions (
    id                     TEXT PRIMARY KEY,
    acquisition_session_id TEXT NOT NULL,
    stage                  TEXT NOT NULL,
    decision               TEXT NOT NULL,
    evaluated_risk         TEXT NOT NULL,
    permissions_json       TEXT NOT NULL DEFAULT '[]',
    production_impact      INTEGER NOT NULL DEFAULT 0,
    rollback_available     INTEGER NOT NULL DEFAULT 0,
    reasons_json           TEXT NOT NULL DEFAULT '[]',
    policy_name            TEXT NOT NULL,
    policy_version         TEXT NOT NULL,
    budget_snapshot_json   TEXT NOT NULL DEFAULT '{}',
    decision_key           TEXT NOT NULL,
    decided_by_process_id  TEXT,
    reviewed_by_event_id   TEXT,
    created_at             TEXT NOT NULL,
    UNIQUE(acquisition_session_id, decision_key)
);
CREATE INDEX IF NOT EXISTS idx_autonomy_decision_session
    ON autonomy_decisions(acquisition_session_id, created_at);

CREATE TABLE IF NOT EXISTS acquisition_attempts (
    id                     TEXT PRIMARY KEY,
    acquisition_session_id TEXT NOT NULL,
    attempt_type           TEXT NOT NULL,
    attempt_number         INTEGER NOT NULL,
    plan_id                TEXT,
    result_id              TEXT,
    status                 TEXT NOT NULL,
    failure_reason         TEXT,
    created_at             TEXT NOT NULL,
    completed_at           TEXT,
    UNIQUE(acquisition_session_id, attempt_type, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_acquisition_attempt_session
    ON acquisition_attempts(acquisition_session_id, attempt_type, attempt_number);

-- Phase 5B: approved extension proposals become isolated, verifiable builds.
CREATE TABLE IF NOT EXISTS construction_plans (
    id TEXT PRIMARY KEY,
    extension_proposal_id TEXT NOT NULL,
    capability_gap_id TEXT NOT NULL,
    work_requirement_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    target_capabilities_json TEXT NOT NULL DEFAULT '[]',
    expected_artifacts_json TEXT NOT NULL DEFAULT '[]',
    verification_requirements_json TEXT NOT NULL DEFAULT '[]',
    sandbox_requirements_json TEXT NOT NULL DEFAULT '{}',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    context_snapshot_id TEXT,
    llm_invocation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(extension_proposal_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_construction_plan_status ON construction_plans(status);

CREATE TABLE IF NOT EXISTS construction_steps (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    inputs_json TEXT NOT NULL DEFAULT '{}',
    expected_outputs_json TEXT NOT NULL DEFAULT '[]',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, step_index)
);
CREATE INDEX IF NOT EXISTS idx_construction_step_plan ON construction_steps(plan_id, step_index);

CREATE TABLE IF NOT EXISTS sandbox_workspaces (
    id TEXT PRIMARY KEY,
    construction_plan_id TEXT NOT NULL UNIQUE,
    root_locator TEXT NOT NULL,
    status TEXT NOT NULL,
    max_files INTEGER NOT NULL,
    max_file_bytes INTEGER NOT NULL,
    max_total_bytes INTEGER NOT NULL,
    timeout_seconds REAL NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS construction_grants (
    id TEXT PRIMARY KEY,
    construction_plan_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL,
    allowed_permissions_json TEXT NOT NULL DEFAULT '[]',
    allowed_root TEXT NOT NULL,
    network_policy TEXT NOT NULL,
    process_policy_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_checks (
    id TEXT PRIMARY KEY,
    construction_plan_id TEXT NOT NULL,
    layer TEXT NOT NULL,
    check_type TEXT NOT NULL,
    check_key TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(construction_plan_id, check_key)
);
CREATE INDEX IF NOT EXISTS idx_verification_plan ON verification_checks(construction_plan_id);

CREATE TABLE IF NOT EXISTS construction_results (
    id TEXT PRIMARY KEY,
    construction_plan_id TEXT NOT NULL UNIQUE,
    extension_proposal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    artifact_resource_ids_json TEXT NOT NULL DEFAULT '[]',
    verification_check_ids_json TEXT NOT NULL DEFAULT '[]',
    provided_capabilities_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    failure_reason TEXT,
    created_at TEXT NOT NULL
);

-- Phase 5C: exact verified artifacts promoted through a separate production
-- grant, smoke verification, activation and rollback boundary.
CREATE TABLE IF NOT EXISTS installation_plans (
    id TEXT PRIMARY KEY,
    construction_result_id TEXT NOT NULL UNIQUE,
    extension_proposal_id TEXT NOT NULL,
    capability_gap_id TEXT NOT NULL,
    work_requirement_id TEXT NOT NULL,
    artifact_versions_json TEXT NOT NULL DEFAULT '[]',
    target_capabilities_json TEXT NOT NULL DEFAULT '[]',
    production_destinations_json TEXT NOT NULL DEFAULT '[]',
    registry_changes_json TEXT NOT NULL DEFAULT '[]',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    rollback_spec_json TEXT NOT NULL DEFAULT '{}',
    component_name TEXT NOT NULL,
    component_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    validation_reasons_json TEXT NOT NULL DEFAULT '[]',
    context_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_installation_plan_status ON installation_plans(status);

CREATE TABLE IF NOT EXISTS installation_steps (
    id TEXT PRIMARY KEY,
    installation_plan_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    inputs_json TEXT NOT NULL DEFAULT '{}',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(installation_plan_id, step_index)
);

CREATE TABLE IF NOT EXISTS installation_grants (
    id TEXT PRIMARY KEY,
    installation_plan_id TEXT NOT NULL UNIQUE,
    allowed_artifact_hashes_json TEXT NOT NULL DEFAULT '[]',
    allowed_destinations_json TEXT NOT NULL DEFAULT '[]',
    allowed_registry_changes_json TEXT NOT NULL DEFAULT '[]',
    allowed_permissions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS installation_checks (
    id TEXT PRIMARY KEY,
    installation_plan_id TEXT NOT NULL,
    check_key TEXT NOT NULL,
    check_type TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(installation_plan_id, check_key)
);

CREATE TABLE IF NOT EXISTS installation_results (
    id TEXT PRIMARY KEY,
    installation_plan_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    installed_artifact_versions_json TEXT NOT NULL DEFAULT '[]',
    activated_capabilities_json TEXT NOT NULL DEFAULT '[]',
    previous_state_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installation_decisions (
    id TEXT PRIMARY KEY,
    installation_plan_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by_process_id TEXT,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    reviewed_by_event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activation_records (
    id TEXT PRIMARY KEY,
    installation_plan_id TEXT NOT NULL UNIQUE,
    component_name TEXT NOT NULL,
    component_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    definition_name TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    artifact_versions_json TEXT NOT NULL DEFAULT '[]',
    artifact_hashes_json TEXT NOT NULL DEFAULT '[]',
    installed_root TEXT NOT NULL,
    previous_state_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(component_name, component_version)
);
CREATE INDEX IF NOT EXISTS idx_activation_definition
    ON activation_records(definition_name, definition_version, status);

CREATE TABLE IF NOT EXISTS rollback_records (
    id TEXT PRIMARY KEY,
    installation_plan_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    restored_state_json TEXT NOT NULL DEFAULT '{}',
    removed_destinations_json TEXT NOT NULL DEFAULT '[]',
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS resources (
    id                 TEXT PRIMARY KEY,
    uri                TEXT NOT NULL UNIQUE,
    resource_type      TEXT NOT NULL DEFAULT 'unknown',
    source_adapter_id  TEXT,
    source_identity    TEXT,
    current_version_id TEXT,
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_versions (
    id                 TEXT PRIMARY KEY,
    resource_id        TEXT NOT NULL,
    version            INTEGER NOT NULL,
    content_hash       TEXT,
    size_bytes         INTEGER,
    source_event_id    TEXT,
    ingress_receipt_id TEXT,
    locator            TEXT NOT NULL DEFAULT '',
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    UNIQUE (resource_id, version)
);
CREATE INDEX IF NOT EXISTS idx_rversion_resource ON resource_versions(resource_id, version);
CREATE INDEX IF NOT EXISTS idx_rversion_hash ON resource_versions(resource_id, content_hash);

CREATE TABLE IF NOT EXISTS resource_representations (
    id                   TEXT PRIMARY KEY,
    resource_version_id  TEXT NOT NULL,
    representation_type  TEXT NOT NULL,
    content_json         TEXT,
    metadata_json        TEXT NOT NULL DEFAULT '{}',
    created_by_process_id TEXT,
    extractor_name       TEXT NOT NULL DEFAULT '',
    extractor_version    TEXT NOT NULL DEFAULT '1',
    created_at           TEXT NOT NULL,
    UNIQUE (resource_version_id, representation_type, extractor_name, extractor_version)
);
CREATE INDEX IF NOT EXISTS idx_repr_version ON resource_representations(resource_version_id);

CREATE TABLE IF NOT EXISTS adapter_checkpoints (
    adapter_id    TEXT NOT NULL,
    stream_key    TEXT NOT NULL,
    cursor        TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (adapter_id, stream_key)
);
"""

#: Columns added to pre-existing tables after they shipped.  ``CREATE TABLE IF
#: NOT EXISTS`` cannot widen a table that already exists, so a database written
#: by an earlier phase needs these added explicitly.  ``(table, column, ddl)``.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("events", "ingress_receipt_id", "TEXT"),
    # Phase 3F: which event triggered this instance, so re-dispatching an event
    # cannot start the same process twice.
    ("process_instances", "trigger_event_id", "TEXT"),
    # Phase 4A: what a unit of work needs, what it turned out to be missing,
    # and which definition was chosen for it.
    ("work_requirements", "required_capabilities_json", "TEXT"),
    ("work_requirements", "missing_capabilities_json", "TEXT"),
    ("work_requirements", "selected_definition_name", "TEXT"),
    ("work_requirements", "selected_definition_version", "TEXT"),
    # Phase 4B: what a composed plan may start from and must produce, and
    # which plan is currently pursuing this need.
    ("work_requirements", "available_input_types_json", "TEXT"),
    ("work_requirements", "required_output_types_json", "TEXT"),
    ("work_requirements", "selected_plan_id", "TEXT"),
    ("process_instances", "plan_id", "TEXT"),
    ("process_instances", "plan_node_id", "TEXT"),
    # Phase 4B.1: an edge now names the ports it connects, so data flow is
    # decided at planning time rather than by iteration order at run time.
    # Nullable: a Phase 4B edge has none, and is treated as legacy.
    ("plan_edges", "output_type", "TEXT"),
    ("plan_edges", "input_type", "TEXT"),
    ("plan_edges", "output_key", "TEXT"),
    ("plan_edges", "input_key", "TEXT"),
    # Phase 4C: a plan is now one candidate among several, so it carries the
    # identity of its own shape and its place in the replanning history.
    ("process_plans", "fingerprint", "TEXT"),
    ("process_plans", "replan_attempt", "INTEGER NOT NULL DEFAULT 0"),
    ("process_plans", "supersedes_plan_id", "TEXT"),
    # What this work is optimising for, how many times it may be replanned,
    # and how many times it has been.
    ("work_requirements", "decision_preference_json", "TEXT"),
    ("work_requirements", "max_replans", "INTEGER"),
    ("work_requirements", "replan_count", "INTEGER NOT NULL DEFAULT 0"),
)


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
        """Create all tables if they do not already exist, then widen old ones."""
        retired = self._retire_legacy_plan_edges()
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        if retired:
            self._restore_plan_edges(retired)
        self.conn.commit()

    # --- plan_edges: a constraint that had to change ------------------------

    #: The Phase 4B uniqueness rule, as a column tuple.
    _LEGACY_EDGE_UNIQUE = ["plan_id", "from_node_id", "to_node_id", "artifact_type"]

    def _retire_legacy_plan_edges(self) -> list[str] | None:
        """Move a Phase 4B ``plan_edges`` aside so the schema can rebuild it.

        Phase 4B allowed one edge per ``(plan, producer, consumer, type)``.
        That is wrong once an edge names ports: a node that produces
        ``m:left`` and ``m:right`` legitimately feeds both inputs of one
        consumer, and under the old rule the second edge was silently dropped
        — the consumer then ran with an input missing and the plan still
        reported success.  Exactly the quiet wrong answer this phase exists to
        remove (spec §14).

        SQLite cannot alter a table constraint, so the table is renamed here
        and its rows copied into the rebuilt one by
        :meth:`_restore_plan_edges`.  Returns the old column names, or ``None``
        when there is nothing to migrate.
        """
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "plan_edges" not in tables:
            return None
        if "plan_edges_legacy" in tables:  # an interrupted migration
            self.conn.execute("DROP TABLE plan_edges_legacy")

        if not self._has_legacy_edge_constraint():
            return None

        columns = [row["name"] for row in self.conn.execute("PRAGMA table_info(plan_edges)")]
        self.conn.execute("ALTER TABLE plan_edges RENAME TO plan_edges_legacy")
        # The renamed table drags its indexes along; drop ours so the rebuilt
        # table can create them under the same names.
        for row in self.conn.execute("PRAGMA index_list(plan_edges_legacy)"):
            if row["origin"] == "c":
                self.conn.execute(f'DROP INDEX IF EXISTS "{row["name"]}"')
        return columns

    def _has_legacy_edge_constraint(self) -> bool:
        """Whether ``plan_edges`` still carries the table-level Phase 4B UNIQUE."""
        for row in self.conn.execute("PRAGMA index_list(plan_edges)"):
            if row["origin"] != "u":  # 'u' = created by a UNIQUE table constraint
                continue
            columns = [
                info["name"]
                for info in self.conn.execute(f'PRAGMA index_info("{row["name"]}")')
            ]
            if columns == self._LEGACY_EDGE_UNIQUE:
                return True
        return False

    def _restore_plan_edges(self, columns: list[str]) -> None:
        """Copy the retired edges into the rebuilt table and drop the old one.

        Only columns the rebuilt table also has are carried over, so a database
        from any earlier phase migrates without knowing which one it came from.
        """
        current = {row["name"] for row in self.conn.execute("PRAGMA table_info(plan_edges)")}
        shared = [c for c in columns if c in current]
        column_list = ", ".join(f'"{c}"' for c in shared)
        self.conn.execute(
            f"INSERT OR IGNORE INTO plan_edges ({column_list}) "
            f"SELECT {column_list} FROM plan_edges_legacy"
        )
        self.conn.execute("DROP TABLE plan_edges_legacy")

    def _add_missing_columns(self) -> None:
        """Add columns introduced after a table first shipped (idempotent).

        Keeps a database written by an earlier phase readable by a later one
        without a migration framework.
        """
        for table, column, ddl in ADDED_COLUMNS:
            existing = {
                row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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
