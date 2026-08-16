# NEXUS SEED

NEXUS SEED is a durable, event-driven runtime for processes that observe,
reason, suspend, resume, act, and extend their capabilities safely.

Its core stays intentionally small:

```text
Event -> Process -> State -> Continuation -> Event -> Resume
```

Only six primitives are fixed: `Event`, `Process`, `State`, `Context`,
`Continuation`, and `Runtime`. Skills, agents, workflows, and observers are
roles of a Process—not additional core abstractions.

*[日本語版](README.ja.md)*

## What is implemented

- SQLite-backed atomic transitions, retries, timers, crash recovery, and restart-safe continuations
- Semantic world state with history and provenance
- Capability-based work matching, multi-process plans, and bounded replanning
- LLM input and external action boundaries with validation, policy, and audit trails
- Durable ingress, resource versioning, extraction, and event delivery
- Capability-gap analysis, sandboxed construction, verification, reviewed production activation, and rollback
- Phase 5D autonomous capability acquisition with `AUTO`, `REVIEW_REQUIRED`, `FORBIDDEN`, and hard budgets
- Phase 5E provider federation for local Processes, directory Skills, and external Agents
- Phase 5G authenticated human commands, durable Goals, Work controls, and complete command audit trails
- Feature-gated Phase 6 Self/Master projections, persistent Intentions, Attention, Experience/Reflection, and finite self-initiated activity
- Authenticated Human Cockpit for Overview, Being, causal Activity, Work, Reviews, Providers, System health, and aggregated Capability Assistance
- Goal-centric Projects: creating a Goal starts its Project, and its Work, status, and situation are derived from existing records
- Read-only Project Chat that explains one project in natural language from its Project Situation
- One Goal-driven loop — Goal, Project, World, Work, Capability, Execution, Evaluation — with a thin coordinator that reports where each Goal stands and asks for help when it cannot continue

`AUTO` never skips safety checks. It still goes through the existing validators,
scoped grants, ActionProposal boundary, verification, activation, and
reconciliation. Runtime/core/policy changes and unrestricted shell or network
access remain forbidden.

## Run the application

NEXUS SEED is an event-processing server, not a chat UI. Install it, configure
`.env`, start the local LLM if used, and then run the durable webhook service.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Set operational and LLM values in `.env`:

```dotenv
NEXUS_SEED_DATA_DIR=C:/Users/user/AppData/Local/nexus-seed
NEXUS_SEED_WEBHOOK_HOST=127.0.0.1
NEXUS_SEED_WEBHOOK_PORT=8787
NEXUS_SEED_WEBHOOK_TOKEN=replace-this-token
NEXUS_SEED_COCKPIT_ENABLED=true

NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1
NEXUS_SEED_LLM_MODEL=exact-model-id
```

Check recovery and configuration once, then start the server:

```powershell
nexus-seed --once
nexus-seed --check-llm
nexus-seed
```

Open `http://127.0.0.1:8787/cockpit`. The browser asks for the same webhook
token and keeps it only in tab-scoped session storage. Cockpit reads existing
projections and traces; controls are submitted exclusively through the Phase
5G `/control` endpoint. The view does not auto-refresh; use the refresh button
to request a new snapshot. Capability Assistance joins the existing Goal →
Intention → Work → CapabilityGap → AcquisitionSession trace and appears only
when automatic acquisition is waiting for review or cannot continue. Set
`NEXUS_SEED_COCKPIT_ENABLED=false` to remove all
Cockpit routes without changing Runtime, webhook, or CLI behavior.

A Project is one Goal plus the Work that Goal generates — one Work is already a
Project. Creating a Goal creates its Project:

```powershell
nexus-seed control '/goal create title="Runtime health" objective="Runtime と LLM の状態を把握する" priority=HIGH'
```

The command answers with the `project_id` it derived from the Goal id, and the
Project appears in the Cockpit Projects view immediately. Only that association
is stored: the title, objective and lifecycle stay on the Goal, so
`/goal pause`, `/goal resume` and `/goal cancel` are the whole project
lifecycle. Work generated for the Goal joins the same Project, and replanned or
restarted Work stays there. Project status is derived in a fixed order —
`CANCELLED`, `PAUSED`, `BLOCKED`, `NEEDS_ATTENTION`, `ACTIVE`, `PLANNING`,
`COMPLETED`, `IDLE` — so the same facts always read the same way.

Explicit association still works for Work (`project=project-a`) and Goals
(`metadata={"project_id":"project-a", ...}`), and a Goal saved outside the
Control Plane stays unassigned rather than being given a Project at read time.
No Project table or Project Runtime is created. Authenticated callers can read:

```text
GET /projects
GET /projects/project-a/situation
```

Both endpoints use the webhook bearer token and reconstruct their response
from the same durable records after every request. Process/LLM handlers can
read the identical projection through
`ctx.services.get_project_situation("project-a")`.

Project Chat answers questions about one project from that same projection:

```text
GET  /projects/project-a/chat
POST /projects/project-a/chat   {"message": "今このプロジェクトは何で止まってる？"}
```

The Cockpit Projects view opens a project and puts the chat panel beside its
situation. The answer is compiled from the Project Situation projection, the
project's own thread and the question — never from SQLite, another project, or
raw traces. This phase is deliberately read-only: a request to cancel,
prioritize, approve or proceed is refused with `READ_ONLY_REFUSED` instead of
being executed, and change still belongs to the `/control` endpoint. Naming a
different project returns `OUT_OF_SCOPE` rather than an answer from it.

Threads survive restart in `project_chat_threads` / `project_chat_messages`.
Chat history is conversation, not confirmed world state, so it never becomes an
Observation, StateDelta or World State fact. Without a configured LLM — or when
the model returns unusable output — the reply is a deterministic summary of the
projection, labelled `LLM_UNAVAILABLE`, `LLM_FAILED` or `LLM_INVALID`, and
Runtime is unaffected.

Submit a natural-language task from another terminal. The command reads the
webhook URL and token from `.env`:

```powershell
nexus-seed task "Analyze this request and determine the required work"
```

Inspect durable state and handle human-review pauses without stopping the
server:

```powershell
nexus-seed status
nexus-seed reviews
nexus-seed review <review-id> approve
```

Phase 5G also provides an authenticated, schema-validated control plane. These
commands bypass natural-language interpretation, but never bypass safety,
permission, action, or autonomy policy:

```powershell
nexus-seed control '/status'
nexus-seed control '/task create objective="Analyze Project A" priority=HIGH cloud_forbidden=true'
nexus-seed control '/pause <work-id>'
nexus-seed control '/resume <work-id>'
nexus-seed control '/provider <work-id> REQUIRE local_runtime'
nexus-seed control '/trace <work-id>'
nexus-seed control '/goal create objective="Make Project A review ready" priority=HIGH'
```

The local control principal and comma-separated grants are configured with
`NEXUS_SEED_CONTROL_IDENTITY` and `NEXUS_SEED_CONTROL_PERMISSIONS`. Goals are
separate durable domain records; `evaluate_goal` discovers deduplicated Work
through the existing event-driven pipeline.

Phase 6 is enabled by default. Set `NEXUS_SEED_PHASE6_ENABLED=false` to restore
Phase 5G behavior. When disabled, no Phase 6 Process is registered and no wake
Event is appended. When enabled, startup may append one `existence_wakeup` only
when an active Goal, unresolved Intention, or unanswered Self question exists.
Goals without explicit success criteria are structurally decomposed after the
Intention exists; the internal `advance_human_goal` fallback is never sent to
Capability Acquisition in Phase 6.
The finite Process chain then returns to the normal idle/event-wait state.

Submit domain Events with inline JSON or `--payload-file`:

```powershell
nexus-seed event measurement_completed --payload-file measurement.json
```

An `accepted` response means the Event is durable; processing continues
asynchronously. `--source-key` supplies a stable external deduplication key.
Restarting the server recovers unfinished delivery, processes, retries, timers,
and continuations from SQLite. See the [Japanese guide](README.ja.md) for the
full command reference and step-by-step procedure.

For development only:

```powershell
python -m nexus_seed.demo
pytest
```

## Repository layout

```text
nexus_seed/core/          fixed data models
nexus_seed/runtime/       routing, scheduling, execution, recovery
nexus_seed/storage/       SQLite persistence
nexus_seed/processes/     concrete Process definitions and handlers
nexus_seed/extension/     Phase 5A capability gaps and acquisition proposals
nexus_seed/construction/  Phase 5B sandboxed construction and verification
nexus_seed/installation/  Phase 5C reviewed activation and rollback
nexus_seed/autonomy/      Phase 5D sessions, policy, budget, trace
nexus_seed/providers/     Phase 5E providers, delegation, skill import, trace
nexus_seed/control/       Phase 5G commands, identities, Goals, and authorization
nexus_seed/presence/      Phase 6 Self/Master/Intention projections and Experience traces
nexus_seed/projects/      read-only Project Situation models and projections
nexus_seed/chat/          read-only Project Chat context, guards, and answers
nexus_seed/orchestration/ Goal-loop status and the human-intervention request
nexus_seed/cockpit/       Human-facing read model and dependency-free Web UI
tests/                    acceptance and restart-convergence tests
```

## Documentation

- [Detailed architecture and phase history](docs/architecture.md)
- [Architecture inventory: every module, in one area of the loop](docs/architecture-inventory.md)
- [詳細アーキテクチャ（日本語）](docs/architecture.ja.md)
- [Contributor invariants and working agreement](AGENTS.md)

Current implementation includes default-on **Phase 6 — Persistent Being** with
a complete Phase 5G compatibility flag. Phase 7 is intentionally out of scope.
