# Redesign Inventory — NEXUS SEED as Project Orchestrator

NEXUS SEED is being narrowed from a system that *executes* work to one that
**decides which Projects exist and delegates each to an Agent**:

```text
ContextManager -> ProjectRouter -> ProjectManager -> AgentManager -> A2AGateway
```

What changes is the **centre of gravity**, not the code inventory. Per the
redesign brief nothing is deleted in this phase. Every module in `nexus_seed/`
is classified below.

## Classification

| Class | Meaning |
| --- | --- |
| **KEEP** | Stays in NEXUS SEED and is part of, or directly serves, the new Core. |
| **MOVE_TO_AGENT_RUNTIME** | Conceptually belongs inside a Project Agent. Still present and working; the new Core does not call it. Physical relocation is a later phase. |
| **DEPRECATE** | Superseded as a *central* mechanism. Not removed, not called by the new Core. |
| **DELETE** | Nothing. No module is deleted in this phase. |

## KEEP — the new Core

| Module | Role |
| --- | --- |
| `orchestrator/models.py` | `Project`, `Agent`, `ProjectAgentConfig`, `RoutingDecision`, `A2AMessage` |
| `orchestrator/context_manager.py` | Compiles what the router may reason over |
| `orchestrator/router.py` | Existing project vs. new Goal, decided semantically |
| `orchestrator/project_manager.py` | Project CRUD, status, priority, relationships, lifecycle |
| `orchestrator/agent_manager.py` | One Project = one Agent: assign, spawn, idle, stop, health |
| `orchestrator/a2a_gateway.py` | The only NEXUS SEED ↔ Agent channel, with audit |
| `orchestrator/agent_runtime.py` | Where an Agent runs (in-process fake, A2A seam) |
| `orchestrator/orchestrator.py` | Wires the five components; owns escalation handling |
| `storage/orchestrator_store.py` | Durable projects, agents, A2A messages |

## KEEP — infrastructure the Core relies on

| Module | Why it stays |
| --- | --- |
| `core/*` | The six primitives are unchanged and still carry everything |
| `storage/database.py` and the generic stores | One SQLite layer, one schema |
| `backends/base.py`, `backends/llm.py` | The router's reasoning boundary; `FakeLLMBackend` keeps tests network-free |
| `providers/a2a.py` | The one A2A client and poll loop; `A2AAgentRuntime` reaches a real Project Agent through it — a second protocol client was deliberately not written |
| `providers/project_agent.py` | The wire side of one whole delegation: PROJECT_ASSIGNMENT out, the eight message types back |
| `control/*`, `storage/control_store.py` | Authenticated human commands and durable Goals — how a person reaches the orchestrator |
| `adapters/*`, `ingress/*`, `ingress_cli.py`, `app.py`, `operations.py` | The outside world still becomes an Event before anything acts on it — and, with `NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED`, a `human_message` becomes Project work through `processes/project_orchestration.py` |
| `cockpit/*` (Projects view) | Reads `orchestrator_projects` directly, shown apart from the Goal-derived projection |
| `chat/*` | Read-only human views; they read projections and change nothing |
| `world/*`, `storage/state_store.py`, `processes/semantic.py`, `intelligence/*`, `processes/llm_interpret.py` | World State is the context the router reasons over; the LLM-proposal boundary is unchanged |
| `context/*` | Declared per-activation views; the same discipline the ContextManager follows |
| `delivery/*`, `resources/*` | Durable delivery and versioned documents, unchanged |

## MOVE_TO_AGENT_RUNTIME — belongs inside a Project Agent

These implement *how a unit of work gets done*. Under the redesign that is the
Agent's business, so the new Core never calls them. They remain in the
repository, tested and working, for the Goal-driven path that predates this
phase.

| Module | Belongs to the Agent because |
| --- | --- |
| `planning/*`, `processes/planning.py`, `storage/plan_store.py` | Task decomposition and multi-step composition is what a Project Agent does with its Goal |
| `decision/*`, `processes/decision.py`, `storage/decision_store.py` | Choosing between candidate plans, and replanning, is in-project reasoning |
| `capabilities/*`, `storage/capability_store.py` | Per-work capability matching — explicitly removed from the centre |
| `providers/registry.py`, `providers/skills.py`, `providers/models.py`, `storage/provider_store.py` | Per-work provider selection and Skill loading are Agent execution concerns (`providers/a2a.py` is the exception and stays: it is the delegation boundary itself) |
| `backends/action.py`, `actions/*`, `processes/actions.py`, `storage/action_*.py` | Touching the outside world is Agent execution, under the Agent's own permissions |
| `extension/*`, `construction/*`, `installation/*`, `autonomy/*` and their processes/stores | Acquiring a missing capability is what an Agent asks for via `NEED_CAPABILITY`; building it is not orchestration |
| `work/*`, `processes/work_intelligence.py`, `storage/work_requirement_store.py` | Work-unit derivation and matching; the orchestrator now tracks Projects, and Tasks inside them belong to the Agent |
| `processes/demo_resistance.py`, `resistance_check`, `work/rules.py` | The wafer/resistance demo domain — Legacy, and the acceptance scenario for suspend/resume |

## DEPRECATE — superseded as a *central* mechanism

| Item | Superseded by |
| --- | --- |
| Work-unit capability search in the centre (`capabilities/matcher.py` driven from `work_intelligence`) | The Agent owns execution; missing capability arrives as a `NEED_CAPABILITY` escalation |
| Work-unit provider selection in the centre (`providers/registry.py` selection path) | `AgentManager.assign_or_spawn`: one Agent per Project, chosen once |
| Work-unit execution management in the centre (`processes/actions.py` dispatch path) | `A2AGateway` delegation; NEXUS SEED does not run the work |
| `orchestration/loop.py`, `orchestration/models.py`, `orchestration/processes.py` | The Goal-loop coordinator is replaced as the entry point by `orchestrator/`; it still serves the pre-existing Goal path |
| `projects/projections.py` as the *only* Project model | `orchestrator/models.Project` is now the durable management record. The derived `ProjectSituation` stays: it answers "what is happening inside this Goal", which the orchestrator record deliberately does not store |

## DELETE

None. No module was deleted in this phase.

## The two Project records, and why both exist

| | `projects/` (pre-existing) | `orchestrator/` (this phase) |
| --- | --- | --- |
| Nature | Derived projection, no table | Durable record, `orchestrator_projects` |
| Identity | `project-<goal-id>`, derived from a Goal | `project-<uuid>`, created by the router |
| Answers | "What is happening inside this Goal?" | "Which projects exist, who owns them, what are they waiting on?" |
| Can express | Work, reviews, intentions, blockers derived from records | `assigned_agent_id`, `parent_project_id`, orchestrator status ladder |

The redesign needs `assigned_agent_id` and `parent_project_id`, which a Goal
cannot carry — hence a stored record. The projection is untouched so all 1255
existing tests keep passing.

## What the new Core deliberately does not do

- Decompose a Goal into Tasks — the Agent does that.
- Search for a Capability per unit of work.
- Select an ExecutionProvider per unit of work.
- Manage tool execution, retries or sandboxes for work.
- Let an Agent create a Project. `DISCOVERED_NEW_PROJECT` is a *report*;
  creation stays with the router.
