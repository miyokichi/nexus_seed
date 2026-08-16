# Architecture Inventory

Taken before the Goal-driven integration phase, so that integration could reuse
what exists instead of rebuilding it. Every module in `nexus_seed/` is placed in
exactly one area of the loop:

```text
Goal -> Project -> World -> Work/Task -> Capability -> Execution -> Evaluation -> Goal
```

Areas 1–6 are the loop. Runtime Infrastructure carries all six and is not one of
them. Interface / Adapter is how people and the outside world reach the loop.

## 1. Goal / Project Management

| Module | Role |
| --- | --- |
| `control/models.py` | `Goal`, priority, constraints, `HumanIdentity`, `Command` |
| `control/service.py` | `ConsoleService`: authorized `/goal` and `/task` commands; creates the Project with the Goal |
| `control/parser.py`, `control/adapters.py` | Deterministic explicit-command parsing; channel mapping |
| `storage/control_store.py` | Durable Goals, identities, commands, results |
| `projects/lifecycle.py` | Goal-rooted project identity (`project_id` derived from the Goal id) |
| `projects/models.py`, `projects/projections.py` | `Project` / `ProjectSituation` / status ladder, all derived |
| `presence/*` | Phase 6 Self, Master and the persistent Intention beneath a Goal |
| `processes/persistent_being.py` | Intention maintenance, attention, reflection as ordinary Processes |

## 2. World Model / Observation

| Module | Role |
| --- | --- |
| `world/observation.py`, `world/state_delta.py` | What was read; what is proposed to change |
| `world/provenance.py` | Walk a current fact back to the raw Event |
| `storage/state_store.py` | Append-only history plus a rebuildable current view |
| `storage/observation_store.py`, `storage/state_delta_store.py` | The journals behind those two records |
| `processes/semantic.py` | `interpret_event` → `apply_state_delta` → `state_changed` |
| `processes/llm_interpret.py`, `intelligence/*` | The LLM reading boundary: proposal, validation, policy |
| `resources/*`, `storage/resource_store.py`, `processes/resources.py` | Documents as durable, versioned, extracted world content |

## 3. Work Planning

| Module | Role |
| --- | --- |
| `work/work_requirement.py`, `work/work_match.py` | The need, and whether it is already covered |
| `work/rules.py` | Deterministic "this change implies this work" rules |
| `work/impact.py`, `work/trace.py` | Consequence record; why this process is running |
| `processes/work_intelligence.py` | `impact → work_required → match → missing → spawn → satisfied` |
| `processes/control.py` | `evaluate_goal`: Goal + criteria + world → gap → Work (also area 6) |
| `storage/work_requirement_store.py` | Durable needs |
| `planning/*`, `processes/planning.py`, `storage/plan_store.py` | Multi-process composition when no single process fits |
| `decision/*`, `processes/decision.py`, `storage/decision_store.py` | Evaluating candidate plans, selecting one, bounded replanning |

## 4. Capability Management

| Module | Role |
| --- | --- |
| `capabilities/*`, `storage/capability_store.py` | What the system can do; matching a need against it |
| `extension/*`, `processes/extension.py`, `storage/extension_store.py` | Gap analysis and acquisition proposals |
| `construction/*`, `processes/construction.py`, `storage/construction_store.py` | Sandboxed build and verification |
| `installation/*`, `processes/installation.py`, `storage/installation_store.py` | Reviewed production activation and rollback |
| `autonomy/*`, `processes/autonomy.py`, `storage/autonomy_store.py` | The bounded coordinator: `AUTO` / `REVIEW_REQUIRED` / `FORBIDDEN`, budgets |

## 5. Execution

| Module | Role |
| --- | --- |
| `providers/*`, `storage/provider_store.py` | Provider federation: internal Processes, directory Skills, external Agents |
| `backends/base.py`, `backends/llm.py` | Swappable reasoning engines |
| `backends/action.py`, `actions/*`, `processes/actions.py` | The only path that touches the outside world, with permissions and risk |
| `storage/action_*.py` | Proposals, decisions, executions |
| `processes/work_intelligence.py::resistance_check`, `processes/demo_resistance.py` | The demo work Process (see Legacy) |

## 6. Evaluation / Replanning

Evaluation has no module of its own — it is three existing mechanisms, which is
why it was easy to miss:

| Mechanism | Where |
| --- | --- |
| Did this Task really finish? | `core/process.py::satisfy_work` + `WorkRequirement.completion_criteria` |
| Is the Goal satisfied by the world now? | `processes/control.py::evaluate_goal` (triggered by `work_satisfied`, `state_changed`, `goal_evaluation_requested`) |
| Is there another way to do it? | `processes/decision.py` replanning, `processes/work_intelligence.py::reconcile_blocked_work` |
| Is the Project complete? | `projects/projections.py::project_status` |

## Runtime Infrastructure (carries all six, belongs to none)

`core/*` (Event, Process, State, Context, Continuation, effects) ·
`runtime/*` (router, scheduler, executor, resolver, drain, clock, services) ·
`delivery/*` and `storage/event_delivery_store.py` (durable delivery) ·
`context/*` (declared per-activation views) ·
`storage/database.py` and the generic stores (`event`, `process`,
`continuation`, `timer`, `join`, `activation`, `context_snapshot`,
`llm_invocation`, `adapter_checkpoint`) · `llm_config.py`.

## Interface / Adapter

`adapters/*` and `ingress/*` (the outside world becomes an Event) ·
`ingress_cli.py`, `operations.py`, `app.py` (CLI and the runnable server) ·
`cockpit/*` (read-only human interface) · `chat/*` (read-only Project Chat) ·
`storage/chat_store.py`, `storage/ingress_receipt_store.py`.

## Orchestration (added by this phase, deliberately thin)

`orchestration/models.py`, `orchestration/loop.py` — where one Goal stands in
the loop, compiled from the areas above and owning none of them.
`orchestration/processes.py` — one Process that states, as an Event, when the
loop cannot turn without a person.

## Unclassified, and what was decided

| Item | Finding | Decision |
| --- | --- | --- |
| `processes/demo_resistance.py`, `resistance_check`, `DEFAULT_WAFER` in `processes/work_intelligence.py` | The wafer/resistance demo domain, wired into the work-intelligence module | Keep as Legacy. It is the acceptance scenario for suspend/resume and is used by many Phase 1–2 tests. It should eventually move to a demo package; moving it now would touch tests this phase must not change. |
| `work/rules.py` | Deterministic domain rules (`expected_work_types`, capability and I/O tables) for that same demo domain | Keep. It is the seam where a deployment states its own rules; the Goal path does not use it, and no second rules engine was added. |
| `work/impact.py` | `Impact` is referenced by `work/__init__` and tests, but no handler constructs one; `impact_analysis` uses `work/rules.py` directly | Keep as documented domain data, not deleted: it is the named record of "a change has consequences" and is exercised by `tests/test_impact_analysis.py`. Do not build a second consequence model. |
| `providers/skills.py` | Directory-skill import; reachable only through explicit registration | Keep. It is how an external Agent becomes an ExecutionProvider — the integration point this phase relies on rather than adding an Agent framework. |
| `demo.py`, `nexus_seed/processes/__init__.py` re-exports | Runnable Phase 1 demo | Keep, documented in the README. |

Nothing was deleted during this phase. Every area above already existed; the
integration added a coordination layer over them, not a replacement for any.
