# AGENTS.md — working agreement for NEXUS SEED

This file is guidance for any agent (human or AI) working on this repository.

## Non-negotiable invariants

1. **The six primitives are fixed.** `Event`, `Process`, `State`, `Context`,
   `Continuation`, `Runtime`. Do not rename or restructure these core models.
2. **One Process primitive.** Do **not** add `Skill`, `Harness`, `Agent`,
   `Workflow`, etc. as separate core abstractions. They are *roles* of a
   Process. If you think you need a new base type, you almost certainly don't.
3. **Definition vs. Instance stays split.** `ProcessDefinition` is static;
   `ProcessInstance` is a running copy.
4. **Runtime is mechanism only.** No domain knowledge, no inference, no LLM in
   the runtime. All intelligence lives in process handlers.
5. **Continuations are logical.** Never persist the Python call stack. A
   suspended process must be fully rebuildable from SQLite.
6. **Restart must be lossless.** Any change must keep
   `tests/test_suspend_resume.py` green: close the runtime, rebuild from the
   database, resume, complete.

## Layout

- `core/` — pure data models, no I/O.
- `storage/` — SQLite persistence (stdlib `sqlite3`, no ORM). JSON stored as TEXT.
- `runtime/` — router, scheduler, executor, continuation_resolver, runtime.
- `processes/` — concrete process definitions + handlers.

## Conventions

- Python 3.12+, `from __future__ import annotations`, type hints on public APIs.
- Public functions/classes get docstrings. Don't swallow exceptions; log them.
- Handlers are `async def handler(ctx: ProcessContext) -> ProcessResult` and
  build results with `ctx.complete(...)`, `ctx.suspend(...)`, `ctx.fail(...)`.
- Emit events via `ctx.new_event(...)` so `correlation_id` / `causation_id`
  chains stay intact.
- Keep dependencies at zero for the library; test-only deps go in `[dev]`.

## Testing

```bash
pytest
python -m nexus_seed.demo
```

Tests must be independent and use a temp SQLite database (`tmp_path`).

## Avoid (over-engineering)

No abstract base-class towers, factory-of-factories, DI frameworks, event-bus
frameworks, microservices, or premature optimization. Phase 1 is a single
Python application.

## Done in Phase 2A (Durable Runtime)

- Atomic process transitions (`Database.atomic`, executor commits one batch).
- Idempotency (idempotent `submit_event` + `process_activations` ledger).
- Crash recovery sweep (`Runtime.recover`, RUNNING → RUNNABLE on startup).
- Retry model (`RETRY_WAIT`, `retry_count`/`max_retries`/`next_retry_at`).
- Timer events (`TimerStore`, `Clock`, `ctx.suspend_on_timer`, `runtime.tick`).
- Spawn / join (`ctx.spawn_and_join`, `JoinStore`, `JoinCoordinator`).

## Done in Phase 2B (Semantic World Model)

- `Observation` / `StateDelta` as domain data under `world/` (NOT core types).
- Two-part world state: `world_state_history` (source of truth, append-only) +
  `world_state_current` (projection, `rebuild_current_state()`).
- Provenance: `get_state_provenance` walks current → history → delta →
  observation → raw event, DB-only.
- `interpret_event` / `apply_state_delta` processes; `state_changed` event.
- Conflict checking as a domain `StateConflict` (not a runtime error).
- State application stays inside the Phase 2A atomic transaction.
- `ctx.observe()` / `ctx.propose_delta()`; `ProcessResult.observations/state_deltas`.
- Invariant: Observation != StateDelta; schema is not 1:1-locked (one event may
  yield many observations; one observation many deltas).

## Done in Phase 2C (Work Intelligence)

- `Impact` / `WorkRequirement` / `WorkMatch` as domain data under `work/`
  (NOT core types); work executes as ordinary `ProcessInstance`s.
- Pipeline processes: `impact_analysis` → `work_matcher` →
  `missing_work_detector` → `work_spawner` → `resistance_check`, connected by
  `work_required` / `work_matched` / `work_missing` / `work_spawned` /
  `work_satisfied` events. Impact analysis never spawns directly.
- `work_key` (includes state version) gives work logical identity →
  idempotency; new version = new work. `work_requirements.work_key` is UNIQUE.
- Deterministic rules + work→process registry in `work/rules.py`; Runtime holds
  no domain rules.
- `ctx.services` (read-only `RuntimeServices`) for cross-cutting reads;
  writes stay declarative via `ProcessResult.work_requirements` /
  `work_requirement_updates`. `ctx.require_work` / `mark_work` / `satisfy_work`.
- `process_instances.work_key` / `work_requirement_id` link work→process;
  `get_work_trace` walks process → requirement → delta → observation → raw event.
- WorkRequirement completion (SATISFIED) is a declarative atomic side effect.

## Done in Phase 3A (Context Compiler / Memory Architecture)

- Memory = the persistent stores (collective name, NOT a new primitive).
  Context = a regenerable per-activation view compiled from Memory.
- `ContextRequirements` (domain data under `context/`) declared on a
  `ProcessDefinition` (persisted as JSON, restored on reload); `None` ⇒ minimal.
- `ContextCompiler` builds a read-only, selective, deterministic
  `ProcessContextView` (`ctx.view`); handlers read it instead of the stores.
- Fresh resume: the executor recompiles context every activation, so resume
  sees CURRENT state, not the suspend-time snapshot (Invariant 13).
- `context_snapshots` table + `runtime.get_context_snapshots` for audit (never
  a resume source).
- `resistance_check` migrated to `ctx.view`; `ctx.services` kept only for
  special explicit queries.

## Runtime invariants (added in Phase 3A — keep them)

- **11.** Processes receive needed info via Context, not by reading whole stores.
- **12.** Context is a regenerable temporary view, never the source of truth.
- **13.** Resume recompiles Context from current state.
- **14.** Continuation and Context are not the same thing.
- **15.** No direct writes from Context to persistent state (writes = ProcessResult).

## Done in Phase 3B (LLM Intelligence Boundary)

- LLM is a swappable `ExecutionBackend` (`backends/`) called from a Process,
  never in the Runtime. `LLMBackend` + `FakeLLMBackend` share one interface.
- LLM output → `InterpretationProposal` (`intelligence/`) → schema + consistency
  validation → `InterpretationPolicy` → ACCEPT/REVIEW/REJECT. A state conflict
  overrides confidence (>= REVIEW). Schema/parse/backend failures are retryable
  (Phase 2A); nothing partial persists.
- Only ACCEPT becomes Observation + StateDelta, reusing the existing
  `apply_state_delta` pipeline (no private LLM write path). REVIEW suspends on a
  normal Continuation waiting for `interpretation_reviewed` (approve/reject/
  modify), restart-safe.
- New: `interpretation_proposals`, `llm_invocations` tables; `observations.
  proposal_id`; `runtime.register_backend` / `get_proposal` / `get_llm_invocation`.
- Deterministic `interpret_event` and LLM `interpret_event_llm` coexist.

## Runtime invariants (added in Phase 3B — keep them)

- **16.** LLM output is never committed directly to World State.
- **17.** LLM output is a Proposal that must pass Validation + Policy.
- **18.** Human review is an ordinary Event + Continuation (no special primitive).
- **19.** A backend never mutates Runtime/domain state.
- **20.** Real LLM and FakeLLM use the same backend interface.

## Done in Phase 3C (Action / Tool Execution Boundary)

- The outbound mirror of 3B. `ActionProposal` / `ActionExecution` /
  `ActionDecisionRecord` / `RiskLevel` are domain data under `actions/` (NOT
  core types); the pipeline is three ordinary Processes in `processes/actions.py`
  (`action_validator`, `action_executor`, plus `write_analysis_result` as an
  action-performing work process), wired by `action_proposed` /
  `action_approved` / `action_rejected` / `action_reviewed` /
  `action_succeeded` / `action_failed`.
- `ctx.propose_action()` is the *only* way a handler reaches the world. Four
  things stay distinct: intention ≠ ActionProposal ≠ approved proposal ≠
  external side effect.
- Validation order: schema → backend capability → permission → risk policy.
  Permissions are flat strings granted on `ProcessDefinition.metadata
  ["permissions"]` (default deny). A proposal that *under-declares* is rejected:
  each backend action type carries mandatory permissions, so declaring
  `required_permissions: []` cannot escalate.
- `ActionPolicy` (risk → APPROVE/REVIEW/REJECT) lives in the validator's
  definition metadata, so risk appetite is configuration; unmapped ⇒ REVIEW.
  Deliberately not merged with `InterpretationPolicy`.
- REVIEW suspends on a normal Continuation waiting for `action_reviewed`;
  `approve` **re-validates** before acting, `modify` creates a new PENDING
  proposal (never a directly-approved one). `root_proposal_id` keeps the
  original waiter attached across a modify-chain.
- Action backends (`backends/action.py`) publish `BackendCapabilities` (a
  deterministic dict, no capability search). `FakeActionBackend` +
  `LocalFileActionBackend` (sandboxed to `allowed_root`, path traversal is a
  permanent non-retryable failure).
- **Safety model = at-most-once per `idempotency_key`**, not distributed
  exactly-once: a SUCCEEDED `ActionExecution` for the key stops re-execution,
  and `LocalFileActionBackend` keeps a durable on-disk journal so the
  "effect landed, commit lost" crash window is covered too. No 2PC.
- Backend failures reuse the Phase 2A retry loop (no bespoke retry). Attempt
  *journals* (`llm_invocations`, `action_executions`) are now persisted on the
  failure path as well as the success path — this also closes the Phase 3B gap
  where a failed LLM call left no trace (`llm_interpret` now returns
  `ctx.retry(...)` after recording the invocation).
- New tables: `action_proposals`, `action_executions`, `action_decisions`.
  New `ProcessResult` fields + `ctx.propose_action` / `update_action_proposal` /
  `record_action_execution` / `record_action_decision`.
- `runtime.get_action_trace()` walks execution → proposal → process → work →
  delta → observation → raw event, plus ContextSnapshot, decisions (permission
  provenance) and `action_reviewed` events.

## Runtime invariants (added in Phase 3C — keep them)

- **21.** A Process never calls a side-effecting backend directly.
- **22.** Every external side effect goes through an ActionProposal.
- **23.** Every ActionProposal passes Permission + Risk Policy.
- **24.** External action results come back as Events.
- **25.** An action backend never changes World State.
- **26.** Failed action attempts stay in the audit journal.
- **27.** The same ActionProposal never produces the side effect twice.

## Done in Phase 3D (External Observation / Ingress Boundary)

- The inbound mirror of 3C, and the stage *before* perception. `IngressEnvelope`
  / `IngressReceipt` / `AdapterCheckpoint` are domain data under `ingress/`
  (NOT core types); `IngressService` is the only thing that turns an outside
  occurrence into an Event.
- **`source_event_key` is the load-bearing idea.** `Event.id` is our name for
  something; `source_event_key` is the world's name for it. Unique index on
  `(adapter_id, source_event_key)` — the *database*, not application logic,
  makes a redelivery impossible to turn into a second Event.
- Adapters decide `source_event_key`; the service never invents one. An
  envelope without one is REJECTED rather than ingested, because a made-up key
  would make every redelivery look new.
- Atomic: receipt + Event + checkpoint commit in one transaction. Delivery into
  the Runtime happens *after* that commit, so a handler blowing up leaves a
  durable, already-deduplicated event rather than a lost occurrence.
- Three adapters: `ManualAdapter` (+ `python -m nexus_seed.ingress_cli`),
  `WebhookAdapter`/`WebhookIngress`/`WebhookServer` (stdlib asyncio HTTP, shared
  -secret auth, duplicate → 200 + `duplicate: true`), `LocalFileAdapter`
  (sha256 fingerprints, per-path checkpoints, `allowed_root` sandbox that
  resolves symlinks). A webhook **never waits** for the work it causes.
- **Checkpoint ≠ Continuation.** A Continuation is where *our* process resumes;
  a Checkpoint is how much of the *world* we have looked at. Separate tables,
  separate meanings; losing a checkpoint costs a re-observation, not lost work.
- **Adapters do not interpret.** `LocalFileAdapter` reports that bytes changed;
  parsing the file is a downstream Process's job. An adapter that quietly parsed
  spreadsheets would be an unreviewable back door into World State.
- Delivery semantics: **at-least-once acquisition + dedup at ingress**. No
  distributed exactly-once, no 2PC.
- `Event` gained one nullable field, `ingress_receipt_id` (`None` ⇒ the event
  originated inside NEXUS SEED). `Runtime` gained `deliver_event()` — the half
  of `submit_event` after the append — plus ingress read queries and
  `get_ingress_trace()` / `get_ingress_trace_by_source_key()`.
- New tables: `ingress_receipts`, `adapter_checkpoints`. `Database.init_schema`
  now also adds columns introduced after a table shipped (`ADDED_COLUMNS`), so a
  database written by an earlier phase stays readable.
- The four idempotency mechanisms stay **separate** (Event.id / work_key /
  action idempotency_key / source_event_key). They are not merged into one
  general mechanism; `test_ingress_duplicate_closed_loop.py` proves they agree.

## Runtime invariants (added in Phase 3D — keep them)

- **28.** External input becomes an Event only through the ingress boundary.
- **29.** An adapter never changes World State.
- **30.** External identity (`source_event_key`) is separate from `Event.id`.
- **31.** A redelivered external occurrence never produces a second Event.
- **32.** Pull/watch observation position is persisted as a Checkpoint.
- **33.** A Checkpoint is not a Continuation.
- **34.** An adapter never interprets the meaning of external data.

## Later-phase candidates (do not build yet)

- Phase 3E+ — Artifact / Resource layer (Excel / PPT / PDF content extraction);
  further adapters (mail, Slack, GitHub, browser); further ExecutionBackends
  (Shell / Claude Code / OpenClaw / MCP); dynamic organization; capability
  registry; self extension. Do NOT build until instructed.
- Carried over, still open: generic sandbox contract shared by ingress and
  action boundaries; hierarchical permissions; compensating actions; external
  action exactly-once; adapter daemonisation; `adapter_errors` journal.
- Context compiler extensions: semantic retrieval, token budget, priority,
  summarization, artifact loading (don't over-abstract yet).
- Richer `waiting_for` matching; pluggable graph state backend; work dependency
  DAG (`depends_on`/`blocks`/`invalidates`).
