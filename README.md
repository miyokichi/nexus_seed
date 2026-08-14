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

`AUTO` never skips safety checks. It still goes through the existing validators,
scoped grants, ActionProposal boundary, verification, activation, and
reconciliation. Runtime/core/policy changes and unrestricted shell or network
access remain forbidden.

## Quick start

Requires Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
```

Run the restart-safe core demo:

```bash
python -m nexus_seed.demo
```

Run the test suite:

```bash
pytest
```

## LLM connection

The default `.env.example` targets an OpenAI-compatible local server. Set its
URL and exact model id, then enable it:

```dotenv
NEXUS_SEED_LLM_ENABLED=true
NEXUS_SEED_LLM_PROVIDER=openai_compatible
NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1
NEXUS_SEED_LLM_MODEL=your-loaded-model
```

LM Studio commonly uses port `1234`; Ollama commonly uses `11434`. No OpenAI
SDK or API key is required unless the local server itself requires one.

Application setup then needs one call after creating the Runtime:

```python
from nexus_seed.llm_config import configure_llm

configure_llm(runtime)
```

This connects the interpreter, plan selector, extension proposer, and sandboxed
construction generator to one backend. Without it, deterministic fallbacks stay
active. Call it again whenever a Runtime is rebuilt. Anthropic remains supported
through `NEXUS_SEED_LLM_PROVIDER=anthropic` and its optional SDK.

## Repository layout

```text
nexus_seed/core/          fixed data models
nexus_seed/runtime/       routing, scheduling, execution, recovery
nexus_seed/storage/       SQLite persistence
nexus_seed/processes/     concrete Process definitions and handlers
nexus_seed/autonomy/      Phase 5D sessions, policy, budget, trace
tests/                    acceptance and restart-convergence tests
```

## Documentation

- [Detailed architecture and phase history](docs/architecture.md)
- [詳細アーキテクチャ（日本語）](docs/architecture.ja.md)
- [Contributor invariants and working agreement](AGENTS.md)

Current implementation stops at **Phase 5D**. Phase 6 work is intentionally
out of scope.
