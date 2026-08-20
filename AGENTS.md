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
- Keep runtime dependencies minimal. `json-repair` is the sole runtime
  dependency and is used only after strict parsing of LLM JSON fails;
  test-only deps go in `[dev]`.

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

## Done in Phase 5E (Capability Provider Federation)

- `Capability`, `ProcessDefinition`, and `ExecutionProvider` are separate.
  A ProcessDefinition remains the semantic contract; `ProviderBinding` says
  who can execute it. Existing definitions receive an automatic, durable
  `local_runtime` INTERNAL binding.
- `ProviderRegistry` and deterministic `ProviderSelector` filter on status,
  health, adapter presence, permissions, priority, trust, cost, latency, load,
  and stable id. Plan selection remains a separate decision.
- External Skill and Agent work uses structured `DelegationRequest` /
  `DelegationResult` plus a durable, idempotent `ProviderInvocation` journal.
  PENDING work suspends on a normal Continuation waiting for
  `provider_result`, and resumes after restart without invoking twice.
- `BLOCKED_PROVIDER` is distinct from `BLOCKED_CAPABILITY`. Provider recovery
  re-offers existing work through `provider_available`; it does not create a
  CapabilityGap or replay the originating Event.
- Failover is permitted only when a provider reports unavailability before
  execution starts. An exception after start is journaled as an unknown/failed
  attempt and never silently delegated a second time.
- External results may yield typed outputs, inert artifacts, or proposals.
  They cannot write World State or authorize Actions directly, and undeclared
  output types fail validation.
- `DirectorySkillAdapter` translates `skill.json` + `SKILL.md` packages into a
  source-neutral `SkillDescriptor`, ProcessDefinition, provider and binding.
  Natural language never establishes capability claims. Permissions are
  checked explicitly, and packages containing scripts require an active Phase
  5C installation.
- New tables: `execution_providers`, `provider_bindings`,
  `provider_invocations`, `provider_selections`, `imported_skills`. Provider
  health and joined provider/skill/delegation traces are queryable.

## Runtime invariants (added in Phase 5E — keep them)

- **124.** Capability / ProcessDefinition / ExecutionProvider stay separate.
- **125.** Skill and Agent are roles, never Core primitives.
- **126.** ProcessDefinition is the source of truth for semantic execution contracts.
- **127.** External Skills and Agents connect as ExecutionProviders.
- **128.** A capability is executable only with an eligible provider.
- **129.** External provider output never commits directly to World State or Actions.
- **130.** External delegation is durably and idempotently tracked.
- **131.** Skill import never bypasses Permission or Installation safety boundaries.
- **132.** One ProcessDefinition may bind several providers.
- **133.** Provider selection and Plan selection remain separate.

## Done in Phase 5G (Human Command & Goal Interface)

- `Command`, `CommandResult`, `HumanIdentity`, `Goal`, structured Work request,
  constraints and provider directives are domain/application data, never Core
  primitives. `ConsoleService` is channel-independent and backs CLI/HTTP plus
  future Discord slash-command mapping.
- Explicit slash commands are parsed deterministically and pass schema,
  unambiguous-target and permission validation. Natural-language
  `CommandProposal` is untrusted; ambiguous targets clarify and high-impact
  proposals wait for confirmation.
- Human Work preserves objective, scope, priority, deadline, resources,
  constraints, completion criteria, provider directive, command and Goal
  provenance on `WorkRequirement`. Provider constraints only narrow
  `ProviderSelector`; they never grant permissions.
- Work pause/restart is durable. Parked ProcessInstances resume RUNNABLE and
  follow the ordinary activation path, so Context is freshly compiled.
  Cancellation stops future Process/Plan/Action/Acquisition stages but does not
  pretend an already-started external effect was interrupted.
- Goal evaluation is an ordinary event-driven Process. Goal-gap Work uses a
  deterministic semantic key, is generated through `work_required`, and is
  reevaluated on state/work events without a daemon or infinite loop.
- New tables: `human_identities`, `commands`, `command_results`, `goals`;
  WorkRequirement gains explicit control metadata and Goal/Command provenance.

## Runtime invariants (added in Phase 5G — keep them)

- **144.** Explicit Command and Natural Language Interaction stay separate.
- **145.** A Command passes schema validation and authorization before execution.
- **146.** An LLM-generated CommandProposal is never committed directly.
- **147.** Human Command never bypasses Safety or Permission Policy.
- **148.** Goal and WorkRequirement stay separate.
- **149.** Goal is an input for discovering needed Work from current state.
- **150.** Goal reevaluation generates Work idempotently.
- **151.** Provider instruction is a ProviderSelector constraint/preference.
- **152.** Pause/resume recompiles fresh Context.
- **153.** Every control Command audits issuer, request, validation and result.

## Done in Phase 6 (Persistent Being)

- Phase 6 is a default-on, feature-gated composition of ordinary Processes and
  existing stores; it adds no primitive, Runtime replacement, agent loop, or
  parallel Memory. `NEXUS_SEED_PHASE6_ENABLED=false` performs no Phase 6
  registration and appends no wake Event, preserving Phase 5G behavior.
- Self and Master are World State projections. Self capabilities are projected
  live from `CapabilityRegistry`, active Goals from the Phase 5G Goal store,
  and Intentions from World State; none are copied into a competing self store.
  Every Master claim retains `OBSERVED`, `INFERRED`, or `CONFIRMED`, confidence,
  and source Event.
- `attention_evaluation` is a finite ordinary Process. Its deterministic
  result is `RELEVANT`, `IGNORE`, `INVESTIGATE`, or `RECONSIDER`; IGNORE is a
  normal completion and creates no Work. Internal Phase 6 projection changes
  are ignored so the loop cannot feed itself.
- Intention is a long-lived `intention:<id>.record` World State schema beneath
  the existing Goal. Its id is deterministic from Goal id and its lifecycle is
  `ACTIVE / WAITING / SATISFIED / BLOCKED / ABANDONED`. Phase 5G still owns
  Goal lifecycle and `evaluate_goal` remains the Goal-gap-to-Work boundary.
- A Phase 6 Goal with no explicit success criteria is decomposed by the existing
  `evaluate_goal` Process through a validated proposal Event before concrete
  Work is created. `advance_human_goal` is a Phase 5G fallback label, never an
  acquirable Capability. Only concrete missing competences reach Phase 5D.
- Experience is an `experience_recorded` Event that links existing Event,
  ContextSnapshot, Goal/Intention/Work, Action and result identities.
  `get_experience_trace` reconstructs the joined view; there is no Experience
  table or new Core type. Reflection is a normal Process and writes lessons
  only through Observation -> StateDelta -> `apply_state_delta`.
- `existence_wakeup` is appended during enabled application bootstrap only
  when durable unresolved state exists. Self-initiated activity then uses the
  existing Work Intelligence, Provider selection, AutonomyPolicy, Grant,
  ActionProposal, Permission, Risk and Review boundaries. Every chain is
  finite and returns to the Runtime's normal idle/event-wait state.
- Phase 6 failures are ordinary isolated activation failures; Phase 5G Control
  Plane remains usable. Explicit human Commands retain priority and authority
  over Goal/Work pause, resume and cancellation.

## Runtime invariants (added in Phase 6 — keep them)

- **154.** Self, Master, Attention, Intention and Experience are not Core primitives.
- **155.** Phase 6 OFF registers nothing and appends nothing; behavior is Phase 5G.
- **156.** Self and Master are projections over existing durable state, not a new Memory.
- **157.** Available capabilities are projected from CapabilityRegistry, never copied.
- **158.** Every Master claim is OBSERVED, INFERRED, or CONFIRMED.
- **159.** Attention is a finite Process and IGNORE may complete without Work.
- **160.** Intention is durable World State beneath, and distinct from, an existing Goal.
- **161.** Goal evaluation remains the only Goal-gap-to-Work path.
- **162.** Experience is reconstructable from existing Events, State and traces.
- **163.** Reflection changes state only through existing validation and StateDelta boundaries.
- **164.** Self-initiated Work and Action never bypass AutonomyPolicy, Grant or Review.
- **165.** Explicit Control Plane Commands retain priority over self-initiated activity.
- **166.** The existence loop is event/timer/Continuation-driven and never a busy loop.
- **167.** Phase 6 restart, retry and delivery reuse existing durability/idempotency guarantees.
- **168.** A Phase 6 activation failure does not make the Phase 5G Runtime unavailable.

## Done in Human Interface / Cockpit

- Cockpit is an optional Human Interface Layer at `/cockpit`, not a Runtime,
  primitive, Memory, Goal system, Work system, or Review system.
- `CockpitService` compiles a read-only snapshot from existing stores, Phase 6
  projections and trace links. Activity groups causal Event chains and reveals
  Process names only in drill-down details.
- Human-readable error text is a presentation paired with, never substituted
  for, the raw error/audit facts.
- Capability Assistance joins the existing Goal → Intention → Work → Gap →
  AcquisitionSession trace. It suppresses human notification while automatic
  acquisition can progress and aggregates actionable gaps without adding a
  store or changing acquisition semantics.
- Every mutation from the UI is an explicit command to the existing Phase 5G
  `/control` endpoint. Self-question answers become an authorized Command,
  durable Event, and ordinary Phase 6 StateDelta pipeline.
- `NEXUS_SEED_COCKPIT_ENABLED=false` removes Cockpit routes. Webhook, Control,
  Runtime and CLI remain available.

## Human Interface invariants (keep them)

- **169.** Cockpit is a projection/interface layer and adds no Core primitive.
- **170.** Reading a Cockpit snapshot never writes SQLite or changes Runtime state.
- **171.** Cockpit Activity groups existing causal records; it is not a new journal.
- **172.** Human-readable summaries never replace raw trace or audit facts.
- **173.** Every Cockpit mutation passes through the existing Control Plane.
- **174.** Cockpit never writes Core or World State directly.
- **175.** Cockpit failure or disablement does not disable Runtime, CLI, ingress, or Control.
- **176.** Cockpit authentication reuses the configured HTTP shared-secret boundary.
- **177.** A capability warning is human-facing only after automatic acquisition needs review or cannot continue.
- **178.** Capability Assistance actions reuse Control Plane, Review and acquisition boundaries; the UI never resolves a gap directly.

## Done in Project Situation Projection

- `ProjectSituation` is reconstructed from existing Goal, Intention, Work,
  Event, World State, Process and Continuation/Review records. It has no table,
  store, Runtime or write path of its own.
- Association reuses `WorkRequirement.project`, explicit `project_id` /
  `project` metadata, durable identifiers and causal/provenance links. Unknown
  ownership is left unknown; no LLM or text similarity assigns membership.
- `ACTIVE / BLOCKED / NEEDS_ATTENTION / IDLE / COMPLETED` and the short summary
  are deterministic presentation results over current durable facts.
- `GET /projects` and `GET /projects/{project_id}/situation` reuse the Cockpit
  bearer-token boundary. Process/LLM handlers read the same projection through
  the read-only `RuntimeServices` facade.

## Project Situation invariants (keep them)

- **179.** Project and ProjectSituation are not Core primitives.
- **180.** Project Situation is a read-only projection; it has no Project store or Runtime.
- **181.** Project membership requires explicit metadata, an existing identifier, or provenance; LLM inference never establishes it.
- **182.** Project status and deterministic summary are derived facts and never replace source audit records.
- **183.** Project projection reads never mutate SQLite, Runtime, Goal, Work, World State or Review state.
- **184.** Restart reconstructs Project Situation from the same durable source records.
- **185.** Project HTTP reads reuse authentication and disappear with Cockpit without disabling Runtime.

## Done in Project Chat

- `POST /projects/{project_id}/chat` and `GET /projects/{project_id}/chat`
  answer questions about one project. The pipeline is
  `human message + project_id -> ProjectSituation -> chat context -> LLM ->
  answer`; the LLM never queries SQLite, Runtime state or another project.
- The context carries exactly the compacted Project Situation, this project's
  recent chat turns and the question. Raw event payloads and unrelated projects
  are left out.
- Read-only means read-only in this phase: no Goal/Intention/Work change, no
  Action, Replan, Capability Acquisition or Review decision, and no Core State
  write. A change request is refused deterministically before the LLM sees it;
  guidance is a later phase.
- A thread is scoped to one `project_id`. Naming another existing project is
  answered as out of scope instead of being resolved from that project.
- Chat lives in `project_chat_threads` / `project_chat_messages`, an append-only
  journal beside `llm_invocations`. It adds no Core primitive and is never read
  as a confirmed fact about the world.
- `ANSWERED`, `READ_ONLY_REFUSED`, `OUT_OF_SCOPE`, `LLM_UNAVAILABLE`,
  `LLM_FAILED` and `LLM_INVALID` are reported to the interface. Without an LLM,
  or after unusable output, the answer is a deterministic projection summary
  labelled as such, and Runtime is unaffected.

## Project Chat invariants (keep them)

- **186.** Project Chat adds no Core primitive and no Runtime of its own.
- **187.** Project Chat never writes Event, World State, Goal, Intention, Work, Process or Continuation records.
- **188.** Project Chat executes no Action, Replan, Capability Acquisition or Review decision; state change belongs to the Control Plane.
- **189.** A Project Chat answer is compiled only from the Project Situation projection, this project's thread and the question.
- **190.** Project Situation remains the source; a chat answer never changes what it projects.
- **191.** A thread is scoped to one `project_id` and never resolves another project's records.
- **192.** Chat history is conversation, never a confirmed world fact.
- **193.** Every question recompiles the current Project Situation; a thread never answers from a stale one.
- **194.** LLM absence, failure or malformed output degrades to labelled deterministic facts and leaves Runtime untouched.
- **195.** Project Chat reuses the Cockpit authentication boundary and disappears with Cockpit without disabling Runtime.

## Done in Goal-Centric Project Lifecycle

- A Project is one root Goal plus the Work that Goal generates. One Work is
  already a Project; no Project store, Runtime or Core primitive exists.
- `/goal create` creates the Project with the Goal. The only thing written is
  the Goal's own `project_id`, derived from the Goal id, so creation, restart
  and re-evaluation all converge on the same single Project. An explicitly
  supplied `project_id` is still honoured, so the Goal API is unchanged.
- `Project.title` / `objective` / lifecycle are read from the root Goal.
  Nothing about the Goal is copied, so renaming, pausing, resuming or
  cancelling it through the Control Plane needs no project-side update.
- Work generated for a Goal carries that `project_id`, and Work reached through
  its `goal_id` belongs to the same Project. Replanned, restarted and
  later-discovered Work converge on it; no LLM inference moves Work.
- `project_status` is a fixed ladder over existing facts:
  `CANCELLED` > `PAUSED` > `BLOCKED` > `NEEDS_ATTENTION` > `ACTIVE` >
  `PLANNING` > `COMPLETED` > `IDLE`. The root Goal's lifecycle outranks its
  Work, and the same facts always give the same status.
- `ProjectSituation` now leads with `project`, `goal` and `current_intention`,
  and names its Work `remaining_tasks` / `blocked_tasks` / `completed_tasks`
  beside the existing fields. Cockpit lists auto-created Projects and opens
  Goal, Intention, remaining/blocked/completed Work, recent activity and the
  read-only Project Chat.

## Goal-Centric Project invariants (keep them)

- **196.** A Project is a Goal-rooted projection, not a Core primitive and not a stored entity.
- **197.** One Goal has exactly one Project, derived from the Goal id and idempotent across restart and re-evaluation.
- **198.** A Project never copies a Goal, Intention or Work; it references them and reads them.
- **199.** The root Goal is the source of a Project's title and objective.
- **200.** A Project has no lifecycle of its own; Goal pause/resume/cancel through the Control Plane is the whole of it.
- **201.** Project status is derived from existing Goal/Work/Review facts in a fixed priority order.
- **202.** Work belongs to the Project of the Goal it was generated for; membership is never assigned by inference.
- **203.** Goals created outside the Control Plane stay unassigned rather than being given a Project at read time.

## Done in Goal-driven integration

- `docs/architecture-inventory.md` places every module in exactly one area of
  the loop — Goal/Project, World Model, Work Planning, Capability, Execution,
  Evaluation — plus Runtime Infrastructure and Interface/Adapter, and records
  what was decided about each unclassified item. Nothing was deleted.
- The loop was already there and is now proved end to end: Goal creation makes
  the Project, `evaluate_goal` turns criteria plus World State into Work,
  `work_matcher` resolves Capability, blocked Work reaches Capability
  Acquisition, execution goes through the Provider boundary, results become
  StateDeltas through the ordinary pipeline, and `state_changed` /
  `work_satisfied` re-evaluate the Goal until the Project completes.
- Evaluation is not a module: it is `satisfy_work` criteria + `evaluate_goal` +
  replanning/reconciliation + `project_status`. The inventory says so rather
  than adding a fourth evaluator.
- `orchestration/` is the only new code, and it is thin. `get_goal_loop` reads
  where a Goal stands (`PLANNING`, `EXECUTING`, `ACQUIRING_CAPABILITY`,
  `HUMAN_REQUIRED`, `BLOCKED`, `EVALUATING`, `ACHIEVED`, `PAUSED`,
  `CANCELLED`) from the subsystems that own those facts.
- `request_human_intervention` closes the one real gap: when automatic
  acquisition stops, it states as `human_intervention_required` what is being
  pursued, which Task stopped, what Capability is missing, what was tried and
  what a person can supply. It emits an Event and changes nothing.
- Agents remain non-primitive: an external Agent is an ExecutionProvider and an
  internal role is a Process. Self-extension stays part of Capability
  resolution rather than a second loop.

## Goal-driven integration invariants (keep them)

- **204.** Orchestration coordinates; it never re-implements Goal, World, Work, Capability, Execution or Evaluation logic.
- **205.** The Goal loop is read from the subsystems that own each fact; it stores nothing of its own.
- **206.** Reading the loop never writes SQLite or changes Runtime state.
- **207.** A human-intervention request is an Event stating existing facts; it changes no Work, Goal, Capability or World State.
- **208.** Event processing and Goal processing are one path: an Event changes the World, the World re-evaluates affected Goals, and Goals produce Work.
- **209.** Self-extension is part of Capability resolution, not a separate loop, and a failed acquisition ends in a human request rather than unbounded retries.
- **210.** Agents are Processes or ExecutionProviders; no Agent is a Core primitive.

## Done in External Agent Runtime (A2A provider + Skill loading)

- `A2AAgentAdapter` is a generic ProviderAdapter: it converts one
  `DelegationRequest` into an A2A `message/send`, polls `tasks/get` to a
  terminal state (`completed` / `failed` / `canceled`), and converts artifacts
  back into a `DelegationResult`. It selects no skills, runs no tools and
  imports nothing from any agent product. No product-specific provider class
  exists; a different remote agent is a configuration change only.
- Durability stays here and execution goes there. The adapter keeps no task
  database: a lost remote task is re-run by the existing Continuation/retry
  policy. Blocking + polling only — SSE and push notification are not built.
- Transport failure before the task starts raises
  `ProviderUnavailableBeforeStart` (so the existing failover rule applies); any
  failure after it started is a journaled failed attempt. Timeout and
  `input-required` send a best-effort `tasks/cancel` and then fail. Agent Card
  problems are provider problems, never a CapabilityGap.
- Structured output is never guessed. When a definition declares an
  `output_schema`, it is sent, and a text-only answer fails instead of being
  parsed or repaired into JSON. Untyped data takes the declared output type
  only when exactly one is declared; otherwise the ambiguity is an error.
- `SkillLoader` scans ordered roots and produces a `SkillCatalog`
  (`get` / `list` / `find_by_capability`). It is not a second durable registry:
  registration still goes through `SkillImporter` into the Capability registry,
  a ProcessDefinition and a ProviderBinding. `skill.json` gained `enabled`,
  `instruction`, `capabilities` shorthand and optional `input_schema` /
  `output_schema`; unknown manifest fields are rejected rather than ignored.
- Precedence is positional (project-local > user/global > built-in). A
  duplicate inside one root is always an error; across roots the first wins and
  records what it shadows, or `on_duplicate: error` refuses. One malformed
  package is a reported failure, not a failed startup — unless `strict`.
- `SkillImporter.import_descriptor` can bind a Skill to an
  already-registered provider, which is how a cognitive Skill reaches an
  external agent without ever naming an endpoint. A Skill whose permissions the
  provider does not declare stays ineligible and says so.
- `federation_config.py` is application wiring (like `llm_config.py`), reading
  `NEXUS_SEED_A2A_ENABLED` / `NEXUS_SEED_A2A_CONFIG` / `NEXUS_SEED_SKILL_ROOTS`
  plus a JSON file of providers, capability→provider bindings and skill roots.
  URLs and token env-var names live in provider records only.
- Five starter Skills ship in `skills/`. They state responsibilities, not large
  prompts, and each one explicitly refuses to name a provider or commit state.

## External Agent Runtime invariants (keep them)

- **211.** Skill / Capability / Provider / Work stay four distinct things: how to think, what can be done, where it runs, what must be done.
- **212.** A Skill never names an endpoint, model, or a remote runtime's own skills; a remote runtime's advertised skills never establish a Capability here.
- **213.** NEXUS SEED owns durability; the remote agent owns execution. No second durable task store is added for external work.
- **214.** External integration is the A2A protocol boundary only — no product-specific import, provider class, REST endpoint or submodule.
- **215.** Typed output is required or refused, never inferred: a missing structure is a provider failure.
- **216.** Skill precedence is deterministic and stated; identical names are never resolved arbitrarily.
- **217.** Loading a Skill never bypasses the permission and installation boundaries that Phase 5E established for imported Skills.

## Done in Project Orchestrator redesign

NEXUS SEED is re-defined as a **Project Orchestrator**, not an execution agent:

```text
ContextManager -> ProjectRouter -> ProjectManager -> AgentManager -> A2AGateway
```

- `orchestrator/` is the new Core. `Project`, `Agent`, `ProjectAgentConfig`,
  `RoutingDecision` and `A2AMessage` are **domain records, not primitives** —
  the six Core primitives are untouched.
- A durable `Project` record (`orchestrator_projects`) carries `goal`,
  `context`, `status`, `priority`, `assigned_agent_id`, `parent_project_id`,
  `summary`, `blockers`, plus the Tasks attached to it. Status ladder:
  `CREATED / ACTIVE / BLOCKED / WAITING_HUMAN / COMPLETED / FAILED / CANCELLED`.
- The pre-existing derived `projects/` projection is unchanged and still
  answers "what is happening inside this Goal". See
  `docs/orchestrator-redesign-inventory.md` for why both exist.
- `InProcessAgentRuntime` keeps every orchestrator test network-free, exactly as
  `FakeLLMBackend` does for the LLM boundary. `A2AAgentRuntime` is the seam for a
  real external Agent Runtime and reuses `providers/a2a.py`.
- Nothing was deleted. Modules that implement *how work gets done* are
  classified `MOVE_TO_AGENT_RUNTIME` and are simply not called by the new Core.

## Project Orchestrator invariants (keep them)

1. **One Project = one Agent.** Never select an executor per unit of work.
   `AgentManager.assign_or_spawn` reuses a live Agent before spawning.
2. **Only NEXUS SEED creates Projects.** `DISCOVERED_NEW_PROJECT` is a report;
   it goes through the ProjectRouter like any other request.
3. **The Agent owns the task breakdown.** Do not re-add Goal decomposition,
   per-work Capability search, or per-work Provider selection to the Core.
4. **A2A is the only channel** between NEXUS SEED and a Project Agent, and both
   directions are recorded.
5. **The router only proposes.** Its decision is schema-checked and validated
   against the real project list; an unknown `target_project_id` falls back to
   creating a project rather than burying the request in an unrelated one.
6. **No backend, no guessing.** Without a reasoning backend the router
   deterministically creates a project — it never string-matches goals.
7. **Project Agents are generic.** One `ProjectAgent` contract configured per
   project; never per-project agent code.
8. **Restart-safe.** Projects, Agents and A2A history rebuild from SQLite
   alone.

## Done in External Project Agent (A2A delegation of a whole Project)

`nexus-seed project "<request>"` now runs the full path end to end against a
real external agent:

```text
request -> ProjectRouter -> Project -> one Agent -> A2A -> external agent
        -> PROJECT_STATUS / PROJECT_COMPLETED / NEED_* -> Project state
```

- `A2AAgentRuntime` is finished and takes a `ProjectAgentTransport`. The
  orchestrator still knows nothing about HTTP or A2A framing; the wire side is
  `providers/project_agent.py`, which reuses `A2AClient`, `A2AEndpoint` and the
  shared `await_task` poll loop rather than adding a second protocol client.
  `providers/a2a.py` gained `await_task` / `A2ATaskUnfinished` / `task_state`,
  and `A2AAgentAdapter` now uses them, so there is exactly one poll loop.
- One delegation is one `message/send` carrying a `PROJECT_ASSIGNMENT` derived
  from `ProjectAgentConfig` (goal, context, constraints, workspace) plus the
  reply schema. Nothing about a project is tracked a second time on the
  transport side.
- ~~The Skills in `skills/` are offered as contracts (`skill_contracts`).~~
  Retired: a Project is delegated as a *goal*, not as a method, so the
  assignment says nothing about how to meet it. The Agent's skills are the
  Agent's own configuration; `skill_contracts`, `ProjectAgentConfig.
  available_skills` and `A2AProjectAgentTransport(skills=...)` are gone.
- `AgentRuntime` gained `attach(config)`: an Agent read back from the database
  is re-adopted before anything is delegated, so a restart never spawns a second
  Agent for the same Project. `AgentManager.assign_or_spawn` and
  `resolve_block` both go through it.
- Transport failure is separated from Project failure. An unreachable or
  non-answering agent raises `AgentUnavailable`, which records
  `metadata["unavailable"]` on the Agent and leaves the Project's status and
  blockers untouched; only a *working* Agent's `NEED_*` blocks a Project.
- `orchestrator_config.py` is application wiring (like `llm_config.py` /
  `federation_config.py`): `NEXUS_SEED_PROJECT_AGENT_RUNTIME` / `_URL` /
  `_TOKEN_ENV` / `_TIMEOUT_SECONDS` / `NEXUS_SEED_PROJECT_WORKSPACE`, reusing
  the existing skill-root and token-by-env-var settings.
- `ProjectOrchestrator.submit()` returns the settled Project next to the
  decision, for callers (the CLI) that must show where the project ended up.
  `handle_request()` is unchanged.
- `nexus-seed task` is untouched. `nexus-seed project` is a separate entry point
  to the orchestrator and blocks until the Agent answers; everything it changes
  is written to SQLite as it happens.
- Tests split by dependency: `InProcessAgentRuntime` for orchestration, a
  scripted local HTTP server for the A2A boundary, and `tests/integration/`
  (skipped unless `NEXUS_SEED_PROJECT_AGENT_URL` is set) for a real agent. A
  plain `pytest` never needs an agent process.

## External Project Agent invariants (keep them)

- **218.** `orchestrator/` never sees HTTP, JSON-RPC or A2A framing; the wire
  format lives in `providers/` behind `ProjectAgentTransport`.
- **219.** One poll loop. Anything that sends an A2A message and waits uses
  `await_task`; no second client, no second set of timeout rules.
- **220.** Agent unavailability is never an escalation. A transport failure
  leaves the Project's status and blockers untouched and is retryable; only a
  working Agent's `NEED_*` blocks a Project.
- **221.** An Agent's answer is required in contract, never repaired. An answer
  carrying no known message type is a failed delegation, not a guess.
- **222.** NEXUS SEED offers Skill contracts and never an order, a plan or a
  per-work provider choice — the Project Agent decides what applies.
- **223.** A runtime is always told about an Agent (`spawn` or `attach`) before
  work is delegated to it, so a restart reuses the recorded Agent.
- **224.** `pytest` never depends on an external agent process; tests needing
  one live in `tests/integration/` and skip themselves.

## Done in Project Orchestrator normal operation

The orchestrator stopped being a separate command and became how NEXUS SEED
handles requests:

```text
CLI / webhook / connector -> Ingress -> human_message -> route_request_to_project
   -> ProjectRouter -> Project + Agent -> durable A2A hand-over
   -> reconcile (tick, and on start) -> COMPLETED / BLOCKED / WAITING_HUMAN
   -> human follow-up -> same Project, same Agent -> COMPLETED
```

- `processes/project_orchestration.py` is one ordinary Process behind
  `NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED`, registered exactly the way Phase 6
  is. Off is a strict no-op. Ingress is untouched: deduplication stays at the
  boundary (one `source_event_key` is one Event is one activation), so the
  router holds no idempotency logic. The Process carries the request across and
  creates nothing itself.
- Handing over and getting the answer are two steps. `AgentRuntime.deliver`
  returns a `Dispatch` (what the Agent said now, plus a `handle` to ask about
  later) and `collect(handle)` asks once — never waits. A Project therefore
  takes as long as it takes without holding anything open.
- `AgentAssignment` lives on the Agent record (`metadata["assignment"]`): kind,
  task id, status, remote handle, attempts, dispatched-at, next-attempt-at. No
  new table, and no copy of the Project — the envelope is rebuilt from the
  Project each time, so a re-send carries it as it stands now.
- `ProjectOrchestrator.reconcile()` is the one loop that moves Projects outside
  a request: re-adopt, collect, retry, re-hand-over. Recovery is not a separate
  path — the application calls the same method on start and on every tick.
- Retry is bounded and widening (5 attempts, doubling to 5 min) and then stops.
  `UNAVAILABLE` is not retried by reconcile; the next thing a person sends
  starts a fresh hand-over. A `RemoteWorkLost` answer (A2A `-32001`) is the one
  case that re-dispatches, because it is proof nothing is still running.
- Blockers are never deleted. Resolving stamps `resolved_at` / `resolved_by`,
  `Project.current_blockers` is what is in the way now, and a terminal status
  resolves rather than clears. A follow-up Task resolves what blocked the
  Project and the envelope carries that blocker, marked resolved, so the
  instruction reaches the Agent against what it was stuck on.
- The router is given BLOCKED and WAITING_HUMAN projects too, and told that a
  request answering one of them is `ADD_TASK_TO_PROJECT`. Without that, a
  person's answer becomes a second project beside the one already waiting.
- Cockpit gained a **Projects** view over `orchestrator_projects` plus a detail
  route (`/cockpit/api/orchestrator/projects/<id>`) showing the Agent, the
  blocker history, the audited A2A channel, and its own ask/instruct thread.
- With the flag on, a `human_message` still goes through the existing
  interpretation path into World State. The flag adds the Project route; it
  does not remove perception.

## Project Orchestrator operation invariants (keep them)

- **225.** Requests reach Projects through the existing Ingress and one
  ordinary Process. No second intake path, and no idempotency logic outside the
  Ingress boundary.
- **226.** Accepting a request and finishing it are separate. Nothing waits on
  an Agent inside a request; what is outstanding is durable.
- **227.** Recovery is the steady-state loop. `reconcile()` is called on start
  and on every tick; there is no separate restart path to keep in step.
- **228.** A hand-over is re-sent only on proof the Agent lost it
  (`RemoteWorkLost`), never on silence — anything vaguer could run the same
  work twice.
- **229.** Retries are bounded and stop. Transport trouble never sets a Project
  to BLOCKED or FAILED.
- **230.** Blockers are history, not just control state: resolved, never
  deleted, and always attributable.
- **231.** A person's answer goes back to the Project that is waiting for it,
  with the same Agent — never to a new Project.
- **232.** ~~Orchestrator Projects and Goal-derived projects are shown apart.~~
  Retired: Goal is gone, so there is one kind of project and it is shown once. Two
  different things sharing a word must not share a list.
## Done in Project Instructions

Each project's chat can now *act*, without weakening the read-only guarantee:

- `POST /projects/{id}/chat` is unchanged and still read-only.
- `POST /projects/{id}/instruct` (`chat/instruct.py`) executes one instruction.
- The LLM only ever **proposes one explicit command**; it never executes.
- `ALLOWED_COMMANDS` allow-lists the verbs, and `project_scope()` restricts
  every target id to the project the instruction came from. `/task` is bound to
  that project. Both checks are deterministic and run before execution.
- Execution is `ConsoleService.execute` — the same authorized, audited path as
  `/control`. Nothing in the chat layer writes Goal/Work/Event/World State.
- An explicit `/command` works with no LLM configured.
- Instruction and outcome are appended to the same durable project thread with
  `INSTRUCTION_EXECUTED` / `INSTRUCTION_REFUSED` / `INSTRUCTION_FAILED`.

## Project Instruction invariants (keep them)

233. **Asking never changes anything.** Keep `chat/service.py` free of write paths.
234. **The model proposes, the guard decides.** Never execute a command because
   the LLM said it was fine; the allow-list and scope check are the guarantee.
235. **A project's instruction box may only touch that project.** Widening scope
   needs a new, explicit decision — not a prompt change.
236. **Change still belongs to the Control Plane.** Route new verbs by adding
   them to `ALLOWED_COMMANDS`, never by writing records from the chat layer.
237. **No LLM must not mean no control.** Explicit `/commands` keep working.

## Done in Orchestrator Project instructions

- `POST /cockpit/api/orchestrator/projects/<id>/instruct` routes one instruction
  through `ProjectOrchestrator.submit(..., origin_project_id=<id>)`.
- `POST /cockpit/api/orchestrator/projects/<id>/unblock` calls `resolve_block`.
- Deliberately **no** allow-list or scope guard here, unlike the Control Plane
  box: the router is the mapping, and the only outcomes are "add a task to this
  Project" or "create a child Project". Nothing executes work in NEXUS SEED, so
  there is no command surface to constrain.
- Cockpit shows the Project's Tasks as the instruction history, and offers
  ブロック解除 only while a Project has unresolved blockers.

## Done in orchestrator idempotency + Control Plane wind-down

- `ProjectOrchestrator.submit(..., request_id=...)` is exactly-once, backed by
  the durable `orchestrator_instructions` ledger (`InstructionLedger`). A
  resend replays the recorded decision: no second routing call, no second task,
  no second delegation. The guarantee lives in `submit`, so every caller gets
  it, not only the Cockpit. Without a `request_id` behaviour is unchanged.
- `Runtime(..., control_enabled=False)` (env `NEXUS_SEED_CONTROL_PLANE_ENABLED`)
  makes `runtime.console` `None`, which removes `/control`, the Cockpit control
  actions and the Control Plane instruction box. Stores, Goal records and the
  whole orchestrator path keep working.

## Wind-down invariants (keep them)

238. **Delegation is not free.** Anything that can hand an Agent a task must be
     idempotent under retry; add the key, do not rely on the client.
239. **Turning the Control Plane off removes the command surface, never data.**
     Guard on `console is None`; never make a store conditional.
240. **Do not grow the Control Plane.** New human actions belong on the
     orchestrator side. `chat/instruct.py` is maintained, not extended.

## Done in Goal decoupling (executor)

- The executor no longer writes Goals. `Runtime.register_result_applier(name, fn)`
  lets a domain commit its own records inside the activation's transaction, and
  `bootstrap_control` registers `control.goals`. The executor does not receive
  `control_store` at all any more.
- `ProcessResult.goals` / `goal_updates` and `ctx.record_goal` / `ctx.update_goal`
  are unchanged: this moves *who applies*, not the handler API (effects §77), and
  adds no generic `Effect(type, payload)` (effects §75).
- Removing the Control Plane is now deleting its module plus its bootstrap call;
  `runtime/executor.py` and `core/process.py` need no edit.
- The applier list is held **by reference**, not copied: domains register during
  bootstrap, which happens after the Executor is built.

## Effect applier invariants (keep them)

241. **The Runtime commits, the domain decides what.** An applier may write only
     its own records, and only from fields already on `ProcessResult`.
242. **Appliers run inside the activation transaction.** A rollback must take the
     domain's records with it.
243. **Registering the same name twice replaces, never duplicates.** A
     re-bootstrapped runtime must not apply the same effects twice.
244. **Do not add a generic effect bag** to satisfy a new domain; give it typed
     fields and an applier, as Goal has.

## Done in Goal decoupling (Phase 6 reads)

- Phase 6 no longer reads Goals. `Runtime.register_pursuit_source(name, fn)` lets
  a domain say what is currently being pursued, and `runtime.active_pursuits()`
  is what `project_self` and the Phase 6 startup wake ask. `bootstrap_control`
  registers `control.goals` (ACTIVE Goals), so behaviour is unchanged today.
- `presence/projections.py` and `processes/persistent_being.py` contain no
  reference to `control_store` at all — a test asserts that, because the whole
  value of the seam is that it stays unbroken.
- Switching the answer from Goals to Orchestrator Projects is one registration
  change (stage 2), not a Phase 6 change. `SelfProjection.active_goal_ids` keeps
  its name for now so stored projections stay readable; renaming it is part of
  stage 2 along with `IntentionRecord.goal_id`.

## Pursuit source invariants (keep them)

245. **Phase 6 asks the Runtime, never a domain store.** New "what are we working
     on?" reads go through `active_pursuits()`.
246. **No source means pursuing nothing.** An empty list is a legitimate answer,
     not a missing dependency — a Control-Plane-off runtime must stay quiet.
247. **One broken source must not blind the rest.** `active_pursuits()` logs and
     continues; it never propagates a domain's failure into Phase 6.
248. **The Runtime does not interpret the identifiers.** It relays them; only the
     registering domain knows whether they are Goals or Projects.

## Done: the command surface is deleted

- `control/parser.py`, `control/service.py`, `control/adapters.py`,
  `chat/instruct.py`, the `/control` route, `runtime.console`, the `nexus-seed
  control` CLI verb and `submit_control_command` are gone. `control/models.py`
  keeps only the Goal domain; `ControlStore` keeps only Goals.
- What the commands gated moved to paths of its own: `nexus_seed/reviews.py`
  (approve/reject), `nexus_seed/questions.py` (self-question answers),
  `nexus_seed/goals.py` (create/end a Goal), and the Project Orchestrator for
  everything that starts or extends work.
- `AppSettings.control_identity_id`/`control_permissions` became `operator_id`
  — who is at the keyboard, recorded as the actor, authorized against nothing.
  `NEXUS_SEED_OPERATOR_ID` is the new variable; `NEXUS_SEED_CONTROL_IDENTITY`
  still works.
- `chat/models.py` keeps its `INSTRUCTION_*` statuses even though nothing
  writes them: an existing chat journal must stay readable.

## Command-surface invariants (keep them)

249. **Do not reintroduce a verb vocabulary.** A new human action is a path
     (like reviews and questions), or it goes to the Project Orchestrator.
250. **A human decision is an Event.** Never a command record, never a
     permission check — the channel is the gate.
251. **Deciding twice must change nothing.** Every human-decision path finds
     nothing waiting on the second call and returns None.
252. **`nexus_seed/goals.py` is scaffolding.** It exists only while Goals do;
     do not grow it into a Goal service.

## Done: Phase 6 holds Intentions about pursuits, not Goals

- `nexus_seed/pursuit.py` names the four things Phase 6 needs about what is
  being pursued: an id, an objective, whether it is live, and what should make
  it reconsider. `PursuitSource` supplies them — `live()` lists, `get()`
  resolves one whether or not it is still live, because an Intention must keep
  describing a pursuit that has just finished.
- `processes/control.GoalPursuits` is today's source. Swapping it for an
  orchestrator-Project source is the whole of the remaining switch.
- `IntentionRecord.goal_id` became `pursuit_id: str` (ids are text now: a Goal
  id is a UUID, a Project id is `project-<uuid>`). `for_goal` → `for_pursuit`,
  `intention_id_for_goal` → `intention_id_for_pursuit`,
  `SelfProjection.active_goal_ids` → `active_pursuit_ids`.
- Compatibility is one-directional and deliberate: `to_dict` still **writes**
  `goal_id` and `from_dict` still **reads** it, so a journal written before the
  rename stays loadable. Event payloads still carry `goal_id` for the same
  reason, with `pursuit_id` alongside.
- Phase 6 with no registered pursuit source now maintains no Intentions. That
  is the intended reading of invariant 246, and a test that wants Intentions
  must register a source.

## Pursuit invariants (keep them)

253. **Phase 6 must not name Goal.** It asks `ctx.services.get_pursuit` /
     `get_active_pursuits`; only the registered source knows what a pursuit is.
254. **A pursuit id is text.** Do not parse it as a UUID above the source.
255. **`get()` must resolve finished pursuits.** An Intention outlives the
     thing it is about; resolving only live ones silently drops it.
256. **Keep reading `goal_id`** in stored records and event payloads for as
     long as journals written before the rename may be loaded.

## Done: Goal is gone

- Deleted: `nexus_seed/control/`, `storage/control_store.py`,
  `processes/control.py` (`evaluate_goal` and the LLM goal decomposition),
  `nexus_seed/orchestration/` (the Goal loop), `projects/lifecycle.py`,
  `nexus_seed/goals.py`, the Goal-derived half of `projects/projections.py`,
  `ProcessResult.goals`/`goal_updates`, `ctx.record_goal`/`update_goal`,
  `ctx.services.get_goal`/`get_active_goals`/`get_control_store`/
  `get_work_for_goal`, and the `goals`, `commands`, `command_results` and
  `human_identities` tables.
- Kept: `review_human_work`, moved to `processes/work_review.py`. Approving
  constrained Work was never a Goal concern — it is a Continuation waiting on
  an event, settled through `nexus_seed/reviews.py` like any other review.
- `projects/projections.py` is now a thin, stable name over
  `orchestrator/situation.py`; `projects/models.py` keeps ProjectSituation
  because its JSON shape is a public surface.
- `WorkRequirement.goal_id` is now `str | None` and unset by anything in-tree.
  The column and the A2A correlation key keep the name because those are wire
  shapes; treat the value as a pursuit id.
- The Cockpit shows Projects once. `snapshot["projects"]` and the **Goal
  Projects** tab are gone; `GET /projects` still answers, over the same records.
- The result-applier seam (invariants 241–244) has no registered applier left.
  It stays: it is what let this deletion be a deletion.

## Goal-removal invariants (keep them)

257. **Do not reintroduce a goal object.** A Project is the goal. Work that
     needs a parent references a pursuit id.
258. **NEXUS SEED does not decompose.** The Agent breaks a Project into tasks;
     nothing in-tree generates Work from an objective.
259. **`projects/projections.py` stays a name, not a second implementation.**
     Compilation belongs beside the records it reads.

## Done: one box per project

- `POST /projects/<id>/message` is the project thread's single input.
  `chat/message.py` routes it: `RoutingAction.IGNORE` means the message needs
  no project work, which is what a question is, so it is answered read-only;
  anything else is applied and handed to the Agent. Both are appended to the
  same chat journal, so a project has one history.
- `ProjectChatService.ask` gained `refuse_state_changes=False` for that one
  caller. Judging the same message twice by two different rules could only
  produce a contradiction; it opens no write path, because that module has
  none.
- `{"act": true}` overrules the judgement — the person's word wins, because
  overruling them would leave no way to act at all. It skips the router (which
  already read this message and was overruled), adds the Task to the project it
  came from, and is idempotent through the instruction ledger under a distinct
  `project_message.act` source.
- `ProjectRouter._fallback` now returns `ADD_TASK_TO_PROJECT` when
  `origin_project_id` is set. An unroutable in-project request used to become a
  *second* project, splitting one goal across two Agents.
- The Cockpit's two panels became one; `/cockpit/api/orchestrator/.../instruct`
  stays as the API for a caller that has already decided.

## One-box invariants (keep them)

260. **The person never classifies their own message.** New input paths decide,
     or they go through `/message`.
261. **One message, one judge.** Do not stack the keyword guard on top of a
     routing decision, in either direction.
262. **Explaining is the safe default.** When meaning cannot be judged, answer;
     never delegate on a guess.
263. **`act` is one bit, not a vocabulary.** Do not grow it into prefixes,
     verbs, or modes.

## Done: configuration says which model is which

- Two models, one of them not configured here at all. `NEXUS_SEED_LLM_*` is the
  model NEXUS SEED *thinks* with; the Project Agent's model lives in the
  Agent's own configuration, and `NEXUS_SEED_PROJECT_AGENT_*` says only where
  the Agent is. `in_process` calls no model whatsoever.
- `skills_config.py` owns Skill discovery. It used to be reachable only through
  the A2A federation settings, which made it look like a property of provider
  federation; a Skill is a procedure offered to whoever executes.
  `federation_config` re-exports `DEFAULT_SKILL_ROOTS` and reads
  `read_roots()`, so nothing that imported it broke.
- `NEXUS_SEED_SKILLS_STRICT` and `NEXUS_SEED_SKILLS_ON_DUPLICATE` were only
  settable through `a2a.json`; they are env settings now too.
- `nexus-seed config` (`config_report.py`) prints the resolved settings grouped
  by purpose, with the Skills actually found and the roots searched. It reads
  only: no database, no connection.
- `.env.example` is laid out in the same groups, with the two-model distinction
  at the top.

## Configuration invariants (keep them)

264. **Never print a secret.** A key is named by the variable holding it; the
     report says `set` or `EMPTY`, never the value.
265. **`nexus-seed config` starts nothing.** No database is created, no
     endpoint contacted. It must stay safe to run against production settings.
266. **One owner per setting.** A group that needs another group's value reads
     that group's module; do not re-read the variable.
267. **Do not add a setting for the delegated model.** It belongs to the Agent.
     If NEXUS SEED ever needs to know, it asks the Agent — it does not
     configure it.

## Done: two agents, two skill sets, two config files

- `NEXUS_SEED_SKILL_ROOTS` is **NEXUS SEED's own** Skills: imported as
  Processes, routed to an A2A provider when one is configured. It no longer
  reaches a Project Agent in any form.
- Removed from the delegation path: `skill_contracts`,
  `ProjectAgentConfig.available_skills`, the `available_skills` key in the
  `PROJECT_ASSIGNMENT` wire object, `A2AProjectAgentTransport(skills=...)`,
  `orchestrator_config.load_skills`, and `ProjectAgentSettings.skills`.
- The Project Agent instruction no longer speaks of "available skills": it says
  the Agent chooses from *its own* skills and that NEXUS SEED does not know
  what they are.
- This is a wire change. An external Agent Runtime that was reading
  `available_skills` from the assignment must read its own configuration
  instead — which is the point.

## Skill-ownership invariants (keep them)

268. **A Project is delegated as a goal, never as a method.** The assignment
     carries goal, context, constraints and workspace. Nothing about how.
269. **NEXUS SEED does not read the Agent's skills.** Not to validate, not to
     log, not to show. If it needs to know, it asks the Agent.
270. **`NEXUS_SEED_SKILL_ROOTS` is NEXUS SEED's own.** Do not route it into
     `orchestrator_config` or the A2A transport again.

## Later-phase candidates (do not build yet)

- Phase 7+ is intentionally not started. Plugin/package discovery and install,
  production source-tree patching, permission escalation, Runtime/Core/Policy
  self-update, learning/RL policy changes, long-horizon compensation,
  multi-machine coordination, role/team ontologies and richer dynamic
  organization remain unbuilt.
- Guidance Thread: turning a Project Chat request into an authorized Control
  Plane change (Work cancellation, prioritisation, direction) is deliberately
  unbuilt. Project Chat explains; it never acts.
- Manual project creation (a Project without a Goal) is deliberately unbuilt.
  Goal creation is the one path that starts a Project.
- Capability `description` becomes usable for LLM planning; `tags` for search.
  Both are stored already and deliberately unused by matching.
- Compensating actions (undoing a completed node's side effects) remain
  unbuilt; Phase 4C replanning deliberately does not compensate.
- Event replay (deliberately *not* durable delivery); per-subscriber delivery
  targets (`event_delivery_targets`); a real DLQ with a UI.
- Office / PDF / OCR extractors (the registry is ready for them); further
  adapters (mail, Slack, GitHub, browser); further ExecutionBackends (Shell /
  Claude Code / OpenClaw / MCP); `resource_links`.
- A2A streaming/SSE and push notification; automatic propagation of a Work
  cancellation into `tasks/cancel` (the hook exists as
  `Runtime.cancel_provider_invocation`, the Control Plane does not call it
  yet); Skill self-modification or generation.
- Carried over, still open: hierarchical permissions; compensating actions;
  external action exactly-once; OS-level daemonisation of `watch_files`;
  `adapter_errors` journal; large-file streaming hash; production webhook
  security. (Phase 3E closed the "shared sandbox contract" item: `ResourceScope`.)
- Context compiler extensions: semantic retrieval, token budget, priority,
  summarization (don't over-abstract yet). Phase 3E deliberately shipped
  `max_items`/`max_bytes` only.
- Richer `waiting_for` matching; pluggable graph state backend; work dependency
  DAG (`depends_on`/`blocks`/`invalidates`).
