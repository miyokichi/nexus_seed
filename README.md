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
- Sandboxed capability construction, verification, reviewed production activation, and rollback
- Phase 5D autonomous capability acquisition with `AUTO`, `REVIEW_REQUIRED`, `FORBIDDEN`, and hard budgets
- Phase 5E provider federation for local Processes, directory Skills, and external Agents

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

Send an Event from another terminal:

```powershell
$headers = @{ "X-Ingress-Token" = "replace-this-token" }
$body = @{
    source_event_key = "manual-20260815-001"
    event_type = "human_message"
    payload = @{ text = "Analyze this request and determine the required work" }
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri http://127.0.0.1:8787/ingress/webhook `
    -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

`202 Accepted` means the Event is durable; processing continues asynchronously.
Restarting the same command recovers unfinished delivery, processes, retries,
timers, and continuations from SQLite. See the [Japanese guide](README.ja.md)
for the full step-by-step procedure.

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
nexus_seed/autonomy/      Phase 5D sessions, policy, budget, trace
nexus_seed/providers/     Phase 5E providers, delegation, skill import, trace
tests/                    acceptance and restart-convergence tests
```

## Documentation

- [Detailed architecture and phase history](docs/architecture.md)
- [詳細アーキテクチャ（日本語）](docs/architecture.ja.md)
- [Contributor invariants and working agreement](AGENTS.md)

Current implementation stops at **Phase 5E**. Phase 6 work is intentionally
out of scope.
