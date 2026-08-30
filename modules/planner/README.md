# nexus-planner

Planning interfaces and proposal generation from Context, Observation, and
Knowledge. This capability module is tracked directly in the NEXUS SEED
repository under `modules/planner`.

## Ownership

This module owns Planning interfaces, Context assessment, goal bridging, and
Planner backend request shaping. It does not own Project execution,
`little_agent` transport, Observer internals, or Knowledge storage.

## Public API

Import the supported API from `nexus_planner`. This module must remain usable
without importing `nexus_seed`.

## Test

From `modules/planner`:

```powershell
uv run pytest
uv run python -c "import nexus_planner; print(nexus_planner.__name__)"
```
