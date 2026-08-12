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

## Later-phase candidates (do not build yet)

- Phase 3C+ — external observation adapters (file/webhook/CLI → Raw Event),
  ExecutionBackend adapters (Claude Code / OpenClaw / MCP), dynamic organization,
  self extension. Do NOT build until instructed.
- Context compiler extensions: semantic retrieval, token budget, priority,
  summarization, artifact loading (don't over-abstract yet).
- Richer `waiting_for` matching; pluggable graph state backend; work dependency
  DAG (`depends_on`/`blocks`/`invalidates`).
