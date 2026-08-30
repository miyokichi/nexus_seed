# nexus-project-manager

Project lifecycle, routing, assignment, workspace grants, and A2A execution
integration for NEXUS SEED. This capability module is tracked directly in the
NEXUS SEED repository under `modules/project_manager`.

## Ownership

This module owns Project and Work models, lifecycle persistence, routing,
Agent assignment, workspace provisioning, and the A2A communication contract.
It does not own Knowledge semantics, Observer acquisition, Planner judgement,
or `little_agent` runtime internals.

## Public API

Import the supported API from `nexus_project_manager`. This module must remain
usable without importing `nexus_seed`.

## Test

From `modules/project_manager`:

```powershell
uv run pytest
uv run python -c "import nexus_project_manager; print(nexus_project_manager.__name__)"
```
