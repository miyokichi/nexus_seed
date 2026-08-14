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

## Done in Phase 3E (Artifact / Resource Layer + Long-lived Observer)

- Three levels kept strictly apart, as domain data under `resources/` (NOT core
  types): `Resource` (what a thing IS, unique by `uri`) → `ResourceVersion`
  (what it CONTAINED, immutable, numbered, content-hashed) →
  `ResourceRepresentation` (what we MADE of it). Collapsing any two loses
  something real — a content-keyed Resource has no history; a Representation on
  the Resource cannot answer "what did the AI read?" after the file changed.
- Pipeline is four ordinary Processes (`processes/resources.py`):
  `resource_indexer` (file events → Resource/Version) → `extract_resource`
  (→ Representations) → `interpret_resource` (→ Observation + StateDelta,
  through the *existing* `apply_state_delta`), plus `watch_files`.
- **Extraction is a Process, never a Runtime feature** (Invariant 38).
  Extractors are pure functions in a deterministic `ExtractorRegistry`
  (`PlainText` / `JSON` / `CSV`). Adding Office/PDF is a registration, not an
  engine change. Adapters still never interpret (3D's Invariant 34 holds).
- Dedup at two levels: a version is not created when `content_hash` matches any
  existing version of that Resource; a Representation's identity is
  `(version, type, extractor_name, extractor_version)` — so improving an
  extractor makes a *new* rendering beside the old one, never a rewrite.
- `ContextRequirements.resources` (`ResourcesReq`) compiles documents into
  `ctx.view.resources` — deterministic selection only (explicit ids/URIs,
  process input, work metadata). **No semantic retrieval, no embeddings.**
  `max_items` / `max_bytes` with `truncate|exclude`; never summarisation.
- **Fresh resume extends to documents** (Invariant 40): a process resumed after
  a file changed reads the current version. The ContextSnapshot pulls the other
  way and records `resource_version_id` + `representation_id`, so what each
  activation actually read stays answerable (Invariant 39).
- **`watch_files` answers 3D's open question** ("who calls `poll()`?") without a
  daemon abstraction: Process + Continuation + Timer + Checkpoint (Invariant
  41). Between ticks it is a row in SQLite. It inherits crash recovery and
  restart safety; a restart resumes the same instance, never a second one.
  `watch_mail` / `watch_git` would be the same shape.
- `ResourceScope` unifies 3C's and 3D's duplicated `allowed_root` checks, with
  read and write as separate powers. Paths only — not a sandbox (no network,
  process or container isolation, no RBAC).
- New: `resources`/`resource_versions`/`resource_representations` tables;
  `ProcessResult.resources` / `resource_versions` / `resource_representations`;
  `ProcessResult.events_to_route` (events already durable from ingress — routed,
  never re-appended); `ctx.adapters` / `ctx.ingress`; `suspend_on_timer`
  gained `also_waiting_for` + `emitted_events` and now carries staged effects.
- Traces join rather than multiply: `get_resource_trace` /
  `get_representation_trace` connect to the ingress and action traces, so an
  external file → ingress → resource → context → action is one chain.

## Runtime invariants (added in Phase 3E — keep them)

- **35.** External persistent things are Resources, with version history.
- **36.** Resource identity and ResourceVersion content are separate.
- **37.** Extraction results attach to the ResourceVersion, not the Resource.
- **38.** Extractors are Processes, not Runtime features.
- **39.** Which ResourceVersion a Context read stays traceable.
- **40.** Resume recompiles resource Context; `latest_only` means latest.
- **41.** A long-lived observer is Process + Continuation + Timer, not a daemon.

## Done in Phase 3F (Durable Event Delivery / Runtime Hardening)

- Closes the gap flagged at the end of 3E. **Event persistence ≠ event
  delivery** (Invariant 42): storing a fact and giving something the chance to
  react to it are separate, and only tracking the second makes "no event is
  ever forgotten" checkable.
- `EventDelivery` (`delivery/`) is Runtime/infrastructure data — not a core
  type and not domain data. One row per event, UNIQUE on `event_id`.
  `PENDING → DELIVERING → DELIVERED`, or `RETRY_WAIT` with exponential capped
  backoff. `FAILED` is deliberately rare (no `max_attempts` by default).
- **`EventStore.append` creates the obligation in the same transaction.**
  Doing it there rather than at each call site is the whole point: no path can
  persist an event and forget to promise it will be routed (Invariant 43).
  Covers submit / ingress / process-emitted / action / timer / join / review /
  resource events uniformly.
- `DurableEventDispatcher` marks DELIVERING in its own commit (so an
  interrupted attempt is visible), then commits **routing + acknowledgement in
  one transaction** — the state "processes created but event unacknowledged"
  cannot exist. Backed up by a routing idempotency guard:
  `process_instances.trigger_event_id` + `ProcessStore.find_by_trigger`.
- `Runtime._drain` alternates dispatch and execution until both are idle.
  Startup runs legacy backfill → stale-DELIVERING recovery → RUNNING sweep.
  `runtime.tick()` and `dispatch_pending_events()` both drive it.
- **`ProcessResult.events_to_route` and `ctx.route_event` are gone.** The
  ingress boundary already commits the event *with* its obligation, so an
  observer no longer carries events onward — which is exactly why the 3E gap
  is closed (Invariant 47). `deliver_event()` is now only an optimization
  (Invariant 46).
- Delivery is **at-least-once**; exactly-once *outcomes* still come from the
  existing per-layer idempotency, which stays separate (spec §73–§74).
  No replay of DELIVERED events; a late-registered definition does not get
  history.
- Legacy DBs: events with no delivery record are backfilled as **DELIVERED**,
  never PENDING — marking them pending would replay a live system's whole
  history. The guarantee starts at events 3F persists.
- New table `event_deliveries`; new column `process_instances.trigger_event_id`
  (via `ADDED_COLUMNS`). Queries: `get_event_delivery`,
  `get_pending_event_delivery_count`, `get_failed_event_deliveries`,
  `get_delivery_health`.

## Runtime invariants (added in Phase 3F — keep them)

- **42.** Event persistence and event delivery are separate concerns.
- **43.** Every event persisted since 3F has a durable delivery record.
- **44.** A persisted, undelivered event is recovered after a restart.
- **45.** Delivery retry never duplicates logical Process / Work / Action.
- **46.** Immediate routing is an optimization, not the durability mechanism.
- **47.** Delivery recovers from the Event Store alone, without re-ingest.

## Done in Phase 4A (Capability Registry / Capability-based Work Matching)

- Work stops finding its implementation by *name* and starts finding it by
  *competence*. `WorkRequirement.required_capabilities` says what doing the
  work takes; `ProcessDefinition.provides_capabilities` says what a process can
  accomplish; `CapabilityMatcher` connects them (Invariants 48–50).
- **Two things called "capability" stay separate** (spec §4):
  `BackendCapabilities` (3C) is what a *tool* can mechanically do
  (`write_file`); `Capability` (4A) is what a *Process* can accomplish
  (`analyze_resistance`). Different registries, deliberately.
- **A capability gap does not cancel the need** (Invariant 51). No capable
  process ⇒ `WorkStatus.BLOCKED_CAPABILITY` + a `capability_missing` event,
  with `missing_capabilities` recorded. Cancelling would throw away a real
  requirement because of a temporary limitation of our own, and acquiring the
  competence later could never revive it. This is the input to self-extension.
- **Three outcomes, kept distinct** (spec §92): `MATCHED_SINGLE_PROCESS`,
  `MISSING_CAPABILITY` (nothing provides it — *we cannot do this at all*), and
  `COMPOSITION_REQUIRED` (all provided, no single process covers them —
  *we cannot do it in one step*). Phase 4A records the third and stops;
  combining processes is 4B (Invariant 54).
- **Deterministic matching only** (spec §26, §99). No LLM, no embeddings, no
  similarity. Ranking: eligibility → score (`capability_priority` + optional
  coverage) → newer definition version → name. Stable across restarts and
  registration order. Descriptions/tags/input-output types are stored but
  **not matched on** — they are for 4B/planning.
- Candidates are only definitions providing ≥1 required capability: a process
  with nothing to do with the work was never in the running, and recording it
  would make the audit grow with the system rather than with the decision.
- **Reconciliation, not replay** (Invariant 52 / spec §98). A new or re-enabled
  capability appends `capability_available`; `reconcile_blocked_work` re-offers
  the *existing* blocked requirements — same ids, same provenance, no raw event
  re-delivered. Registering only appends, so Phase 3F's delivery obligation
  carries it even if startup crashes before draining.
- Re-registering an unchanged definition announces nothing (spec §83), so a
  restart does not churn through all blocked work.
- **Legacy path coexists** (spec §16–§17): work with no declared capabilities
  falls back to `WORK_PROCESS_REGISTRY`. `resistance_check` /
  `write_analysis_result` now declare `analyze_resistance` /
  `generate_analysis_report`; the D1_CD scenario reaches the same process, only
  the selection principle changed.
- Disable, never delete (spec §42). Disabling affects future matching only;
  running and suspended processes are untouched (spec §43).
- New tables: `capabilities`, `process_capabilities`, `capability_work_matches`
  (attempts accumulate, never overwrite). New `work_requirements` columns for
  required/missing capabilities and the selected definition. New
  `ProcessResult.work_matches` / `capability_matches`.
- The capability registry is the system's **self-model** — what it can do — as
  distinct from World State, which is what it knows about the outside
  (spec §88–§89). No `SelfModel` primitive was added.

## Runtime invariants (added in Phase 4A — keep them)

- **48.** A WorkRequirement may declare the capabilities it requires.
- **49.** A ProcessDefinition may declare the capabilities it provides.
- **50.** Capability matching lives in the work/capability domain, not the Runtime.
- **51.** A missing capability never cancels the need.
- **52.** New capabilities trigger reconciliation of blocked work, not replay.
- **53.** Only a single process covering everything is auto-spawned.
- **54.** Phase 4A never composes several processes.

## Done in Phase 4B (Dynamic Process Composition / Durable Process Plan)

- Takes 4A's `COMPOSITION_REQUIRED` — every competence exists but scattered —
  and works out an order in which several processes add up to the job.
  `ProcessPlan` / `PlanNode` / `PlanEdge` are domain data under `planning/`.
- **A PlanNode is a position, not an execution** (Invariant 56). It names a
  ProcessDefinition; a `ProcessInstance` is still the only thing that runs.
  Execution reuses the existing spawn / join / continuation machinery, so
  atomicity, crash recovery and activation idempotency come for free.
- **Deterministic, bounded backward chaining** (Invariant 64). Goals are the
  required capabilities plus required output types; providers are chosen, their
  inputs become new goals, and `SearchBounds` (nodes / depth / candidates)
  guarantees an answer rather than a hang. Connection is exact symbolic type
  equality (Invariant 59) — no subtyping, no ontology, **no LLM**.
- **Plans are DAGs** (Invariant 58); cycles are rejected. A loop belongs inside
  a process, expressed with a continuation.
- **Nothing runs a plan the planner merely proposed** (Invariant 60) — the same
  boundary as 3B/3C. Validated twice: at composition, and again before each
  stage spawns, because a definition can be disabled in between.
- **A completed node is never re-run** (Invariant 61). `(plan_id, node_key)` is
  UNIQUE, the spawn binds the node to its instance in the same transaction, and
  a restart resumes from the first unfinished position.
- **Failure does not undo** (spec §55) and **does not cancel the need**
  (Invariant 63). No compensation was invented.
- **All nodes complete ≠ work satisfied.** `evaluate_plan_satisfaction` checks
  capability coverage *and* that the required output types were actually
  produced — a plan whose processes all ran but produced nothing required is
  COMPLETED and **not** SATISFIED.
- **`ProcessResult` restructured without breaking anything** (spec §70–§77):
  the ~22 flat lists stay (every handler and test keeps working) and
  `result.effects` / `result.lifecycle` are grouping *views* over them
  (`SemanticEffects`, `WorkEffects`, `ActionEffects`, `ResourceEffects`,
  `PlanningEffects`, …). No generic `Effect(type, payload)` (spec §75).
- **Effect conflicts are refused, not resolved by list order** (Invariant 65).
  `check_conflicts` runs before the commit transaction: identical updates
  collapse, contradictory ones raise `EffectConflictError` and fail the
  activation cleanly. This closes the exact Phase 4A bug where a capability
  selection silently overwrote a work status set in the same handler.
- `TypedOutput` (`{"outputs": [{"type": ..., "value": ...}]}`) is additive: a
  handler returning a plain dict still works and simply contributes nothing to
  data flow. Outputs bind to the next stage by type.
- New tables `process_plans` / `plan_nodes` / `plan_edges`; new columns on
  `work_requirements` (`available_input_types`, `required_output_types`,
  `selected_plan_id`) and `process_instances` (`plan_id`, `plan_node_id`).
  `WorkStatus.PLANNED` added. `WORK_IO_TYPES` in `work/rules.py`.

## Runtime invariants (added in Phase 4B — keep them)

- **55.** Multi-process composition is a durable ProcessPlan.
- **56.** A PlanNode references a ProcessDefinition; it is not an execution.
- **57.** A validated plan's structure is not silently rewritten.
- **58.** Phase 4B plans are DAGs.
- **59.** Process connection is exact symbolic input/output type equality.
- **60.** Planner output is validated before it runs.
- **61.** A COMPLETED PlanNode is never re-run after a restart.
- **62.** Each logical PlanNode converges on one logical ProcessInstance.
- **63.** A failed plan never cancels the need.
- **64.** Phase 4B does not use an LLM to generate plans.
- **65.** Contradictory staged effects fail the activation; no last-write-wins.

## Done in Phase 4B.1 (Composition Hardening / Explicit Binding / Bounded Drain)

Purely structural: no new capability, no LLM, no replanning, no delegation, no
compensation. It closes three things Phase 4B left unsafe to build on before
4C adds judgement.

- **An edge *is* the data flow** (Invariant 66). Phase 4B recorded a
  dependency and let the executor pick a value by type at run time, so with two
  producers of one type the consumer got whichever was visited first — the plan
  could not explain its own data flow and two runs could differ.
  `PlanEdge` now names `output_type` / `output_key` / `input_type` /
  `input_key`; `_resolve_inputs` follows edges instead of building a
  type-keyed dict. Nothing is decided at execution time that was not decided at
  planning time.
- **`Port`** — `"type"` or `"type:key"`, parsed wherever a capability declares
  its input/output types, so the capability model itself did not change.
  A keyless port is a wildcard **on the producing side only**; on the consuming
  side the declaration must match exactly, because the port's name is the dict
  key the handler is given.
- **Ambiguity is a validation failure, never a choice** (Invariant 68). The
  planner leaves an unbindable input unbound and carries the reason in
  `PlanCandidate.ambiguous_bindings`; the validator fails it as
  `AMBIGUOUS_BINDING` naming the input and every candidate producer.
  `BindingStatus` also distinguishes `MISSING_INPUT`, `DUPLICATE_BINDING`,
  `TYPE_MISMATCH` and `INVALID_PORT` — "too many producers" and "no producer"
  are different diagnoses.
- **One producer per consumer input** (Invariant 67), enforced by a partial
  UNIQUE index, not only by the validator. `save_edge` no longer uses
  `INSERT OR IGNORE`: swallowing a conflict is how a consumer ends up running
  with an input missing while the plan reports success.
- **`plan_edges` was rebuilt.** The Phase 4B table UNIQUE
  `(plan_id, from_node_id, to_node_id, artifact_type)` silently dropped the
  second edge when one node feeds two keyed inputs of another — a real
  data-loss bug found while testing this phase. SQLite cannot alter a
  constraint, so `Database.init_schema` detects the old autoindex, renames the
  table, lets the schema rebuild it, and copies the rows back (idempotent).
- **Branching DAGs are an acceptance case, not an assumption.** Phase 4B
  implemented parallel spawn and multi-input join and only ever ran a straight
  line. `P1 → {P2, P3} → P4` now runs, fans out, joins, and survives a restart
  with one branch finished and the other interrupted mid-activation.
- **Bounded drain** (`runtime/drain.py`). `DrainBudget(max_dispatches,
  max_activations, max_cycles)` and `DrainResult`; `runtime.drain(budget)` plus
  budget arguments on `submit_event` / `tick` / `run_pending`, and
  `runtime.default_drain_budget` / `runtime.last_drain`. **Reaching a budget is
  not a failure** (Invariant 71): nothing is FAILED, nothing is dropped, and
  the next call continues (Invariant 73). The default is unlimited, so every
  pre-4B.1 caller behaves exactly as before.
- The dispatch/execute halves **alternate**, so neither can eat the whole
  budget — a long delivery queue cannot starve execution.
- **A budget is a call parameter, not stored state.** What remains lives in
  `event_deliveries` / `process_instances` / `continuations` / `plan_nodes` /
  `timers`; a restarted runtime is told nothing about how the last one was
  paced, and the outcome does not depend on where the slices fell.
- **Old plans are honoured where clear and refused where not** (spec §58).
  `resolve_legacy_binding` accepts an `artifact_type`-only edge when exactly
  one interpretation exists; otherwise the plan is BLOCKED before it runs.
  Guessing would reproduce the very ambiguity this phase removed.
- **Binding provenance in the trace** — `PlanTrace.bindings` (`PlanBinding`:
  producer node/port → consumer node/port), `binding_pairs`,
  `bindings_into(node_key)`, and `inputs_given` read back from the instances.
  Phase 4B could say what ran in what order; the question worth answering is
  *where did this value come from*.

## Runtime invariants (added in Phase 4B.1 — keep them)

- **66.** A PlanEdge names the producing port and the consuming port; data flow
  is decided at planning time, never at execution time.
- **67.** Each consumer input has exactly one producer — schema-enforced.
- **68.** An ambiguous binding fails validation; the planner never picks one.
- **69.** A port's type must match exactly; a key disambiguates but never
  converts.
- **70.** A pre-4B.1 plan runs only where its binding is unambiguous.
- **71.** Reaching a drain budget is not a failure and loses nothing.
- **72.** One runtime call need not reach quiescence.
- **73.** Whatever a slice did not finish is durable and resumes on the next
  call — including after a restart.

## Done in Phase 4C (Plan Selection / Decision Policy / Replanning)

- Deterministic composition persists every structurally distinct valid
  candidate as `PROPOSED`; `PlanFingerprint` removes duplicate shapes without
  using generated ids.
- `PlanEvaluator` derives cost, critical-path latency, maximum risk, minimum
  quality and product reliability from definition metadata. Unknown values
  remain unknown; unknown risk receives a conservative value.
- `DecisionPreference` separates hard limits from soft weights.
  `DeterministicPlanSelector` provides a total, restart-stable order and is
  always sufficient when no LLM selector is installed.
- `LLMPlanSelector` only produces a `PlanSelectionProposal` naming a member of
  the validated shortlist. `SelectionValidator` rechecks membership,
  fingerprint, hard constraints and enabled definitions immediately before
  selection. Hallucinated or drifted choices execute nothing.
- Medium-confidence proposals suspend on an ordinary Continuation waiting for
  `plan_selection_reviewed`; approve and choose-alternative revalidate after a
  restart. Plan review never replaces Action permission/risk review.
- Evaluations, proposals, invocation attempts, `PlanSelection`s and
  `ReplanAttempt`s are durable audit records. The selected-plan pointer,
  selection, plan status and emitted events commit in one atomic activation.
- Terminal plan failure emits durable `replan_required`. Replanning reads the
  current registry, definitions and freshly compiled related World State,
  excludes failed fingerprints, creates a new immutable plan and never cancels
  the WorkRequirement. Exhaustion becomes `BLOCKED_PLAN` and emits
  `replan_unavailable`.

## Runtime invariants (added in Phase 4C — keep them)

- **74.** An LLM cannot create or select outside the validated candidate set.
- **75.** Plan generation and plan selection are separate.
- **76.** Confidence never overrides a hard constraint.
- **77.** LLM unavailability falls back to deterministic selection.
- **78.** Plan selection history is append-only.
- **79.** Terminal plan failure never cancels the need.
- **80.** Replanning creates a new plan; it never rewrites the failed one.
- **81.** Replanning uses current State and the current Capability Registry.
- **82.** Replanning is bounded by `max_replans`.
- **83.** Plan approval never substitutes for Action approval.

## Done in Phase 5A (Self Extension Foundation / Capability Acquisition Proposal)

- Takes 4A's `BLOCKED_CAPABILITY` — which was true and completely inert — and
  makes it the start of a question. `CapabilityGap` / `ExtensionProposal` /
  `ProposedComponent` / `AcquisitionCandidate` / `ExtensionDecisionRecord` are
  domain data under `extension/` (NOT core types); the pipeline is two ordinary
  Processes in `processes/extension.py` (`analyze_capability_gap`,
  `reconcile_capability_gaps`).
- **A need is not a deficiency** (Invariant 85). A `WorkRequirement` is what the
  world asks of us; a `CapabilityGap` is what we lack in ourselves. Separate
  tables, separate lifecycles — the need keeps its id, status and provenance.
- **A deficiency is not a permission** (Invariant 84). Phase 5A generates no
  code, writes no repository, runs no shell, installs no plugin, registers no
  capability or definition and grants no permission (Invariant 90).
  `test_extension_no_activation.py` pins that boundary as a set of zeroes.
- **APPROVED ≠ acquired** (Invariant 89). An approved proposal moves the gap to
  `PROPOSAL_APPROVED`, never `RESOLVED`: the work stays BLOCKED_CAPABILITY,
  because deciding how to acquire a competence is not acquiring it. Only a
  capability actually becoming available RESOLVES a gap.
- **Reuse before construction** (Invariant 88), as arithmetic rather than
  advice: `STRATEGY_ORDER` ranks REGISTER → CONFIGURE → CONNECT_BACKEND →
  ADD_ADAPTER/EXTRACTOR → ADD_PROCESS_DEFINITION → PLUGIN → CODE_EXTENSION, and
  candidates sort by that rank. The analyzer looks at disabled providers,
  configurable definitions, registered backends, the extractor registry and a
  static plugin catalog *before* anything proposes new code.
- `CapabilityMatcher.provides()` was added because `registry.is_provided()`
  answers a narrower question: a capability whose only provider is a **disabled
  definition** has a provider on paper and none in practice. Self-extension has
  to ask the practical question — otherwise the cheapest extension there is
  (re-enable what we already wrote) is invisible.
- **Deterministic analysis first** (spec §25). `CapabilityAcquisitionAnalyzer`
  produces `AcquisitionCandidate`s with no model involved; acquisition hints
  (`resource_type`, `backend_actions`, `ingress_source`, `plugin`) are
  *declared* on a CapabilityRequirement or Capability — no name-similarity, no
  aliasing (spec §75).
- **The LLM elaborates a route; it never invents one** (Invariant 87).
  `LLMExtensionProposer` is shown the gap and the candidate strategies only
  (never the whole registry), and is asked for a title, a description and a
  component decomposition — **no shell commands, no patches, no source code**
  (spec §32–§33). Targets come from the gap, not the answer. An unknown
  strategy, an out-of-candidate strategy, an unknown component type, a widened
  target or an under-declared permission is INVALID. With no proposer, or a
  failing one, the deterministic builder writes the proposal (spec §82).
- **Risk is computed, not claimed** (spec §40–§41). `classify_risk` maps
  strategy → LOW/MEDIUM/HIGH, and only ever raises the floor: unknown strategy
  or component ⇒ CRITICAL, `repository.modify`/`process.execute`/
  `plugin.install`/`network.unrestricted` ⇒ ≥ HIGH, `runtime.modify`/
  `permission.modify` or a declared core/policy change ⇒ CRITICAL.
- **`ExtensionPolicy` is conservative by default** (spec §42–§43):
  LOW/MEDIUM/HIGH → REVIEW, CRITICAL → REJECT, `require_human_approval=True`.
  It lives on the analyzer's definition metadata (like `ActionPolicy`), and a
  bootstrap with no policy argument now *keeps* the recorded one rather than
  resetting it on restart. Human `approve` re-validates against the current
  world and then skips the risk gate; `modify` creates a **new** proposal with
  `root_proposal_id` / `replaces_proposal_id` and never overwrites the original
  (Invariant 92).
- Idempotency by two logical keys, enforced in the schema: `capability_gaps
  (work_requirement_id, missing_key)` and `extension_proposals(fingerprint)`
  (gap + strategy + targets + components). A redelivered `capability_missing`
  finds the gap and the proposal it already made.
- **Reconciliation, not replay** (as in 4A): `capability_available` /
  `backend_available` / `extension_environment_changed` resolve gaps whose
  capabilities became available, or ask for re-analysis of the ones that did
  not — same gap row, same id, same provenance. An approved proposal is never
  silently superseded by a re-analysis.
- `UNSUPPORTED` is a real answer (spec §65–§66): with no route this architecture
  can express, no proposal is invented — the gap is kept and
  `capability_acquisition_unavailable` is emitted.
- New tables `capability_gaps` / `extension_proposals` / `extension_decisions`;
  new `ProcessResult` fields (`capability_gaps`, `capability_gap_updates`,
  `extension_proposals`, `extension_proposal_updates`, `extension_decisions`)
  grouped as `result.effects.extension`, with conflict checking for both update
  kinds. New `ctx.open_capability_gap` / `update_capability_gap` /
  `record_extension_proposal` / `update_extension_proposal` /
  `record_extension_decision`. Queries: `get_capability_gap(s)`,
  `get_open_capability_gaps`, `get_extension_proposal(s)`,
  `get_extension_decisions`, `get_capability_gap_trace`, `get_extension_trace`,
  `get_extension_health` (whose `approved_not_constructed` only grows in this
  phase, and whose `capabilities_acquired` is always 0).

## Runtime invariants (added in Phase 5A — keep them)

- **84.** A missing capability is not permission to modify the system.
- **85.** A CapabilityGap is a deficiency of ours, kept apart from the need.
- **86.** Self-extension happens only through an ExtensionProposal.
- **87.** LLM output never constructs or activates an extension.
- **88.** Reuse of existing capability / process / backend precedes new code.
- **89.** An APPROVED ExtensionProposal is not an acquired capability.
- **90.** Phase 5A changes no repository, runtime or permission.
- **91.** A critical extension is never approved automatically.
- **92.** Extension and review history is append-only.

## Done in Phase 5B (Sandboxed Capability Construction)

- An `APPROVED` `ExtensionProposal` is consumed by three ordinary Processes:
  `plan_extension_construction` -> `execute_extension_construction` ->
  `verify_extension_construction`. They stop at a durable
  `ConstructionResult.VERIFIED`; no installer consumes the verified event.
- `ConstructionPlan` is HOW, kept apart from the proposal's WHAT/WHY.
  Deterministic templates cover `ADD_EXTRACTOR`, `ADD_PROCESS_DEFINITION` and
  `REGISTER_EXISTING_PROCESS`. Proposal/attempt and plan/step logical keys make
  re-delivery, bounded drain and restart converge without repeated writes.
- `ConstructionValidator` runs before workspace creation and refuses a
  non-APPROVED source, target drift, unknown/production-changing steps,
  absolute or traversing paths, incomplete artifact/verification declarations,
  network access, and permissions outside the Proposal ceiling.
- Each plan gets a dedicated non-production `SandboxWorkspace` and a separate
  `ConstructionGrant`. The grant only carries `sandbox.read`, `sandbox.write`
  and `sandbox.test`, defaults network to DENY, and is revoked after checking.
  It never changes global or ProcessDefinition permissions.
- Artifact writes reuse Phase 3C. The Process creates `ActionProposal`s; the
  normal validator applies permissions/risk; `ConstructionActionBackend`
  rechecks plan/workspace/grant/path/size at authorization and execution. An
  escape is rejected before human review and permanently refused by the backend.
- Optional LLM code generation shares `ExecutionBackend`. Its JSON is checked
  for exact relative paths, known roles, count, per-file bytes and total bytes
  before any action exists. No generated shell/install command, patch
  application or automatic repair loop exists.
- Generated files reuse the artifact layer: `Resource` -> immutable
  `ResourceVersion` -> `generated_source` Representation, with plan, proposal,
  workspace, Process and artifact-role provenance.
- Verification is a separate Process with three required layers: structural,
  syntax/import plus structured sandbox tests, and structured
  `CapabilityContract` behavior. Missing dependencies BLOCK without host
  installation. Passing tests without behavior evidence cannot produce VERIFIED.
- VERIFIED seals the workspace and revokes the grant. Gap and Work remain
  blocked; Capability Registry, production definitions and repository stay
  unchanged. `get_construction_trace` joins the whole evidence chain.
- 5A lifecycle cleanup is closed: superseded/cancelled review continuations are
  removed and their suspended instances complete; cancelled Work cancels its
  Gap/live Proposals and prevents unstarted construction.
- New tables: `construction_plans`, `construction_steps`,
  `sandbox_workspaces`, `construction_grants`, `verification_checks`, and
  `construction_results`. Writes are typed as `result.effects.construction`.

## Runtime invariants (added in Phase 5B — keep them)

- **93.** Extension approval is not construction action permission.
- **94.** Construction always goes through a ConstructionPlan.
- **95.** Construction side effects are confined to its SandboxWorkspace.
- **96.** A ConstructionGrant is scoped and changes no global permission.
- **97.** LLM-generated artifacts are never written directly to production.
- **98.** Generated artifacts are Resource / ResourceVersion records.
- **99.** Construction and verification are separate Processes.
- **100.** Passing tests alone never proves a Capability.
- **101.** VERIFIED never means INSTALLED or ACTIVE.
- **102.** Phase 5B changes no Capability Registry or production definition.
- **103.** Extension approval never bypasses Action permission or risk policy.

## Done in Phase 5C (Installation / Activation / Production Promotion)

- A `VERIFIED` ConstructionResult is consumed by ordinary Processes:
  `plan_extension_installation` -> human `installation_reviewed` ->
  `install_extension` -> `verify_installed_extension` ->
  `activate_installed_extension`. `VERIFIED`, `INSTALLED` and `ACTIVE` are
  separate durable states; an installed-but-unsmoked component is invisible to
  capability matching.
- `InstallationPlan` binds immutable `ResourceVersion` ids plus their explicit
  `sha256-...` content hashes to allowlisted, versioned paths under the
  dedicated `installed_extensions/<component>/<version>/` root. Source bytes
  are rehashed before review approval, before copy and again before activation.
- Production authority is a separate one-plan `InstallationGrant`, scoped to
  exact artifact hashes, destinations, registry mutations and permissions.
  A ConstructionGrant is never consulted. Core/policy mutation, unrestricted
  shell/network, arbitrary package installation and arbitrary destinations are
  validation failures.
- Production copies and rollback reuse Phase 3C `ActionProposal` validation,
  permission/risk decisions, execution journal and idempotency keys.
  `InstallationActionBackend` repeats Grant/hash/path checks both before review
  and at execution; approving a plan cannot authorize another Action.
- Installation is version-coexistent rather than overwrite-based. Post-install
  checks separately record hash, module/load, expected interface,
  manifest/metadata, permission declaration and a side-effect-free smoke
  fixture. Failure requests an installation-specific, idempotent rollback;
  the prior active version is never disabled.
- Activation is a typed atomic effect: active component provenance,
  ProcessDefinition/provider linkage, Capability rows, plan/result activation
  and `capability_available` events commit together. Generated Process roles
  run through one generic installed handler; installed extractors are rebuilt
  from durable ACTIVE records after restart.
- `capability_available` reuses the Phase 4A blocked-work reconciliation. The
  original WorkRequirement keeps its id and runs through normal matching. Its
  CapabilityGap becomes RESOLVED only after that Work reaches SATISFIED.
- New tables: `installation_plans`, `installation_steps`,
  `installation_grants`, `installation_checks`, `installation_results`,
  `installation_decisions`, `activation_records`, and `rollback_records`.
  Writes are grouped as `result.effects.installation`, with normal conflict
  detection. `get_installation_trace` joins approval, exact artifacts,
  production Actions, checks, rollback/activation and reconciliation.

## Runtime invariants (added in Phase 5C — keep them)

- **104.** VERIFIED / INSTALLED / ACTIVE are distinct.
- **105.** Installation identity is verified ResourceVersion + content hash.
- **106.** A ConstructionGrant is never production authority.
- **107.** Production changes require InstallationPlan + InstallationGrant.
- **108.** Installation approval does not grant arbitrary Action permission.
- **109.** Install completion alone never publishes a Capability.
- **110.** Activation requires successful post-install verification.
- **111.** Component availability and Capability Registry activation converge atomically.
- **112.** Installation/activation failure does not break an existing active capability.
- **113.** An active Capability traces to its exact verified artifacts.

## Done in Phase 5D (Autonomous Capability Acquisition Loop)

- `CapabilityAcquisitionSession` is the durable coordinator above 5A/5B/5C;
  it does not replace `CapabilityGap`, `ExtensionProposal`,
  `ConstructionPlan/Result`, `InstallationPlan/Grant`, or activation evidence.
- `advance_capability_acquisition` is an ordinary Process. It advances one
  event boundary at a time and survives restart through SQLite state and normal
  durable Event delivery.
- `AutonomyPolicy` is deterministic and yields exactly `AUTO`,
  `REVIEW_REQUIRED`, or `FORBIDDEN`. The conservative default automatically
  handles only LOW-risk exact reuse of an existing ProcessDefinition. New
  ProcessDefinitions/extractors require review. Runtime/core/permission/policy
  mutation, unrestricted shell/network, and out-of-scope code/plugin routes are
  forbidden and cannot be human-overridden.
- AUTO is an approval source, not a bypass: it emits the existing 5A/5C review
  Events and still passes revalidation, scoped Grants, ActionPolicy, exact-hash
  checks, layered verification, production smoke and rollback boundaries.
- `AutonomyBudget` is snapshotted on each Session. Extension depth,
  acquisitions per Work, construction attempts and installation attempts are
  finite; optional risk/cost/time ceilings fail closed. A logical redesign
  attempt is separate from Phase 2A transient retry, and every old plan/result
  remains in the audit trail.
- Recursive dependencies become ordinary WorkRequirements with parent Session
  and depth metadata. Capability availability wakes the parent; an ancestral
  logical requirement stops with `ACQUISITION_CYCLE`.
- `acquisition_key` is order-independent and version-aware. Identical gaps
  share one Session through durable subscribers; capability activation uses
  the existing reconciliation path to satisfy every subscribing Work.
- Cancelling the last subscriber closes pending autonomy review and stops
  future stages. Capability arrival by another route does the same without
  deleting or rolling back an already-active capability.
- New tables: `capability_acquisition_sessions`, `acquisition_subscribers`,
  `autonomy_decisions`, `acquisition_attempts`. Writes are typed as
  `result.effects.autonomy` and participate in normal effect-conflict checks.
  Queries include session/active/blocked/decision/attempt/health and
  `get_acquisition_trace`.

## Runtime invariants (added in Phase 5D — keep them)

- **114.** Autonomous acquisition never bypasses existing validators or policies.
- **115.** AUTO is a policy decision, not an absence of review evidence.
- **116.** REVIEW_REQUIRED is a normal Event + Continuation boundary.
- **117.** FORBIDDEN cannot be overridden by a human approval Event.
- **118.** Every acquisition is bounded by a durable budget snapshot.
- **119.** Recursive acquisition carries parent identity and finite depth.
- **120.** An acquisition cycle stops; it never expands recursively forever.
- **121.** Identical logical gaps share acquisition work without losing Work provenance.
- **122.** Construction redesign attempts and transient Runtime retries stay distinct.
- **123.** Activation returns through capability reconciliation; acquisition never satisfies Work directly.

## Later-phase candidates (do not build yet)

- Phase 6+ is intentionally not started. Plugin/package discovery and install,
  production source-tree patching, permission escalation, Runtime/Core/Policy
  self-update, learning/RL policy changes, long-horizon compensation,
  multi-machine coordination, role/team ontologies and richer dynamic
  organization remain unbuilt.
- Capability `description` becomes usable for LLM planning; `tags` for search.
  Both are stored already and deliberately unused by matching.
- Compensating actions (undoing a completed node's side effects) remain
  unbuilt; Phase 4C replanning deliberately does not compensate.
- Event replay (deliberately *not* durable delivery); per-subscriber delivery
  targets (`event_delivery_targets`); a real DLQ with a UI.
- Office / PDF / OCR extractors (the registry is ready for them); further
  adapters (mail, Slack, GitHub, browser); further ExecutionBackends (Shell /
  Claude Code / OpenClaw / MCP); `resource_links`.
- Carried over, still open: hierarchical permissions; compensating actions;
  external action exactly-once; OS-level daemonisation of `watch_files`;
  `adapter_errors` journal; large-file streaming hash; production webhook
  security. (Phase 3E closed the "shared sandbox contract" item: `ResourceScope`.)
- Context compiler extensions: semantic retrieval, token budget, priority,
  summarization (don't over-abstract yet). Phase 3E deliberately shipped
  `max_items`/`max_bytes` only.
- Richer `waiting_for` matching; pluggable graph state backend; work dependency
  DAG (`depends_on`/`blocks`/`invalidates`).
