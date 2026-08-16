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
- Authenticated Human Cockpit for Overview, Being, causal Activity, Work, Reviews, Providers, and System health

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
to request a new snapshot. Set `NEXUS_SEED_COCKPIT_ENABLED=false` to remove all
Cockpit routes without changing Runtime, webhook, or CLI behavior.

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
nexus_seed/cockpit/       Human-facing read model and dependency-free Web UI
tests/                    acceptance and restart-convergence tests
```

## Documentation

- [Detailed architecture and phase history](docs/architecture.md)
- [詳細アーキテクチャ（日本語）](docs/architecture.ja.md)
- [Contributor invariants and working agreement](AGENTS.md)

Current implementation includes default-on **Phase 6 — Persistent Being** with
a complete Phase 5G compatibility flag. Phase 7 is intentionally out of scope.
