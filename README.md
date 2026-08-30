# NEXUS SEED

NEXUS SEED is a durable Project Orchestrator with a provenance-aware Knowledge
Runtime. It observes authorized input, records what it knows, decides which
Project should exist, delegates execution to one Project Agent, and brings the
result back into Knowledge.

```text
Observation -> Knowledge -> Planning -> Project -> Agent/A2A
     ^                                            |
     +--------------- Result ---------------------+
```

NEXUS SEED owns Project identity, lifecycle, persistence, review, and the
audited Agent channel. The Project Agent owns task decomposition, tool choice,
and execution.

*[日本語版](README.ja.md)*

New users should start with the Japanese
[getting-started guide](docs/getting-started.ja.md). It covers configuration,
the first request, Cockpit, resources, A2A little_agent, and troubleshooting.

## Quick Start

### Requirements

- Python 3.12 or 3.13 (the project defaults to 3.13)
- [uv](https://docs.astral.sh/uv/) (recommended)
- Git submodules initialized for the four NEXUS modules and `little_agent`

Python 3.14 is intentionally excluded because Semantica's current `gensim`
dependency does not publish a compatible Windows wheel.

```bash
git submodule update --init --recursive
uv sync --extra dev --extra semantica
cp .env.example .env
```

PowerShell equivalent:

```powershell
git submodule update --init --recursive
uv sync --extra dev --extra semantica
Copy-Item .env.example .env
```

The default data directory is `~/.nexus_seed`. If you set
`NEXUS_SEED_DATA_DIR`, it must point outside this source repository.

Review the resolved configuration without printing secrets:

```bash
uv run nexus-seed config
```

Start the application:

```bash
uv run nexus-seed
```

Open `http://127.0.0.1:8787/cockpit`. If prompted, enter the value of
`NEXUS_SEED_WEBHOOK_TOKEN` from `.env`. Stop the server with `Ctrl+C`.

To recover and drain durable work once without serving HTTP:

```bash
uv run nexus-seed --once
```

## Common Operations

Keep one `nexus-seed` server running for normal operation, then use another
terminal for commands.

```bash
uv run nexus-seed task "Analyze resources/sales.csv and explain the decline"
uv run nexus-seed status
uv run nexus-seed project "Run this request and wait for its result"
```

| Command | Purpose |
| --- | --- |
| `nexus-seed` / `nexus-seed serve` | Run the durable application and Cockpit |
| `nexus-seed task TEXT` | Submit a request asynchronously through Ingress |
| `nexus-seed project TEXT` | Route a request and wait for the Project by default |
| `nexus-seed status` | Show delivery, Process, and Project status |
| `nexus-seed config` | Show resolved settings with secrets redacted |
| `nexus-seed --check-llm` | Make one request to the configured reasoning model |

Use `--env-file PATH` to load a dotenv file other than `.env`.

## What Runs

### Durable application loop

The long-lived application persists Events, Process state, Knowledge,
Resources, Projects, A2A messages, checkpoints, and review decisions in
SQLite. Restarting with the same data directory resumes outstanding work.

```text
Ingress -> durable Event delivery -> Knowledge update -> Project routing
        -> Project Agent -> result/review -> Knowledge update
```

External occurrences enter through Ingress and are deduplicated by their
source identity. Files are observed only under
`NEXUS_SEED_DATA_DIR/resources` unless an operator explicitly grants a Project
access to another configured root.

### Finite closed loop

The application flow in `nexus_seed.app.flows` provides two bounded APIs:

- `run_once(request)` performs exactly one Observer-to-result pass.
- `run_until_stable(request, max_iterations=3)` replans only after an explicit
  `world_facts` result changes the Knowledge World View.

`run_until_stable` stops on `no_action`, `stable_world`, `blocked`,
`repeated_state`, or `max_iterations`. It is a finite application API, not a
daemon or an unbounded retry loop.

```python
report = await application.run_until_stable(request, max_iterations=3)
print(report.stop_reason, report.project_ids)
```

Raw execution results remain in Knowledge history. Only structured facts such
as the following update the World View:

```json
{
  "world_facts": [
    {"entity": "performance_report", "attribute": "status", "value": "created"}
  ]
}
```

### Semantic Knowledge with Semantica

Canonical YAML v0.1 is the stable document boundary for semantic Knowledge.
It keeps business concepts as entities, scalar values as properties, and
source assertions separate from inferred relations. Install the optional
Semantica integration and ingest the included sample:

```bash
uv sync --extra dev --extra semantica
uv run nexus-seed-semantica \
  --snapshot ~/.nexus_seed/semantica-knowledge.json \
  --ontology modules/knowledge/samples/ontology_v0.1.yaml \
  ingest modules/knowledge/samples/assumption_v0.1.yaml
uv run nexus-seed-semantica \
  --snapshot ~/.nexus_seed/semantica-knowledge.json \
  query "GenX WL Width"
```

The persisted snapshot can be queried after restart without another server.
Set `NEXUS_SEED_SEMANTICA_SNAPSHOT` and
`NEXUS_SEED_SEMANTICA_ONTOLOGY` in `.env` to omit those options. Company use
replaces only the source-to-Canonical converter, sample YAML, and small
ontology; the `nexus_knowledge` query and NEXUS planning flow stay unchanged.

## Configuration

`.env.example` is the canonical list of application settings. `.env` and all
other `.env.*` files are ignored by Git; only `.env.example` is tracked.

### Application

| Variable | Default | Notes |
| --- | --- | --- |
| `NEXUS_SEED_DATA_DIR` | `~/.nexus_seed` | Must be outside the source repository |
| `NEXUS_SEED_WEBHOOK_HOST` | `127.0.0.1` | A non-loopback host requires a token |
| `NEXUS_SEED_WEBHOOK_PORT` | `8787` | Webhook and Cockpit port |
| `NEXUS_SEED_WEBHOOK_TOKEN` | empty | Recommended locally; required beyond localhost |
| `NEXUS_SEED_TICK_SECONDS` | `1` | Runtime polling interval |
| `NEXUS_SEED_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `NEXUS_SEED_COCKPIT_ENABLED` | `true` | Enables `/cockpit` |
| `NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED` | `true` | Enables the durable Knowledge loop |
| `NEXUS_SEED_KNOWLEDGE_POLL_SECONDS` | `60` | Knowledge/resource polling interval |

### Reasoning model

The `NEXUS_SEED_LLM_*` settings configure NEXUS SEED's own reasoning for
routing, assessment, and questions. They do not configure an external Project
Agent's model.

LLM support is optional. With `NEXUS_SEED_LLM_ENABLED=false`, persistence,
Ingress, Knowledge recording, Projects, and Cockpit still run, but semantic
decisions are limited.

For an OpenAI-compatible local server:

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1
NEXUS_SEED_LLM_MODEL=local-model
NEXUS_SEED_LLM_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=local-server-key
```

`NEXUS_SEED_LLM_API_KEY_ENV` contains the name of the environment variable
holding the key, never the key itself.

### Project Agent

`NEXUS_SEED_PROJECT_AGENT_RUNTIME` selects where Projects execute:

- `in_process`: deterministic, local, and restricted; useful for development.
- `a2a`: delegates the complete Project to an external Agent Runtime.

When using `a2a`, set `NEXUS_SEED_PROJECT_AGENT_URL`. The external Agent owns
its model and Skills; NEXUS SEED sends the Project goal and authorized
resources, not an implementation method.

### Resource grants

Host access is closed by default. Configure roots that may be granted:

```dotenv
NEXUS_SEED_PROJECT_RESOURCE_READ_ROOTS=C:/work/shared;C:/work/specs
NEXUS_SEED_PROJECT_RESOURCE_WRITE_ROOTS=C:/work/shared/out
```

The separator is `;` on Windows and `:` on Unix. Write roots should also be
inside the readable scope. A read grant points to the original; writable input
is staged in the Project workspace and collected only after acceptance.

```powershell
uv run nexus-seed-knowledge grants --db path/to/nexus_seed.db
uv run nexus-seed-knowledge grant --db path/to/nexus_seed.db PROJECT_ID `
  "file:C:/work/shared/input.csv" --reason "Input requested by the Project"
uv run nexus-seed-knowledge collect --db path/to/nexus_seed.db PROJECT_ID
```

## Architecture Boundaries

The six core primitives remain fixed: `Event`, `Process`, `State`, `Context`,
`Continuation`, and `Runtime`. Project, Agent, Knowledge, Resource, Work, and
Capability are domain records or Process roles, not new core primitives.

```text
Knowledge Runtime       What does NEXUS SEED know about the world?
Project Orchestrator    What work should exist and what state is it in?
Project Agent           How should one Project be executed?
Runtime                 How is durable execution resumed and delivered?
```

Extracted modules do not import NEXUS SEED or one another. NEXUS SEED composes
them through stable contracts:

```text
modules/knowledge
modules/observer
modules/planner
modules/project_manager
modules/little_agent       external Agent, reached through A2A
```

## Development

```bash
uv run --extra dev pytest
uv run --extra dev pytest tests/test_module_boundaries.py
uv run --extra dev python -m nexus_seed.app --once
```

Tests use temporary SQLite databases. External-Agent integration tests live in
`tests/integration/` and skip unless an Agent endpoint is configured.

Repository overview:

```text
nexus_seed/app/             application composition and CLI
nexus_seed/core/            the six fixed data primitives
nexus_seed/runtime/         durable routing, execution, resume, and delivery
nexus_seed/platform/        cross-module contracts
nexus_seed/resources/       Resource/Version/Representation infrastructure
modules/                    independently owned capability repositories
tests/                      unit, boundary, restart, and E2E tests
```

## Documentation

- [Documentation index (Japanese)](docs/README.ja.md)
- [Getting started (Japanese)](docs/getting-started.ja.md)
- [Semantica guide (Japanese)](docs/semantica.ja.md)
- [Troubleshooting (Japanese)](docs/troubleshooting.ja.md)
- [MVP application flow](docs/mvp.md)
- [Development invariants](AGENTS.md)
