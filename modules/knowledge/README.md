# nexus-knowledge

Knowledge domain, ledger, query, projection, and consolidation services for
NEXUS SEED. This capability module is tracked directly in the NEXUS SEED
repository under `modules/knowledge`.

## Ownership

This module owns Knowledge models, the append-only ledger, query and projection
helpers, consolidation, principles, and its public adapters. It does not own
Planner decisions, Project lifecycle, Observer adapters, or the application
composition root.

## Public API

Import the supported API from `nexus_knowledge`. NEXUS SEED may wire explicit
adapter classes from this package, but this module must remain usable without
importing `nexus_seed`.

## Test

From `modules/knowledge`:

```powershell
uv run pytest
uv run python -c "import nexus_knowledge; print(nexus_knowledge.__name__)"
```
