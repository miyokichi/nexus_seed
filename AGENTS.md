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

## Later-phase candidates (do not build yet)

- Richer `waiting_for` matching (ranges, predicates) and indexed resolution.
- Context persistence and smarter `build_context` selection (keep world-state /
  work APIs decoupled from runtime internals — Phase 2B/2C already do).
- Pluggable state backend behind the `(entity, attribute, value)` shape (graph).
- Work dependency DAG (`depends_on`/`blocks`/`invalidates` — room left in
  `WorkRequirement.metadata`).
- The first real intelligence boundary: an LLM-backed process handler (Phase 3).
