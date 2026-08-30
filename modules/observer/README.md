# nexus-observer

External observation, ingress envelopes, sources, and Observer adapters for
NEXUS SEED. This capability module is tracked directly in the NEXUS SEED
repository under `modules/observer`.

## Ownership

This module owns observation source records, ingress envelopes, receipts,
checkpoints, and manual/file/webhook adapter contracts. It does not own
Knowledge persistence, Planning, Project lifecycle, or application composition.

## Public API

Import the supported API from `nexus_observer`. This module must remain usable
without importing `nexus_seed`.

## Test

From `modules/observer`:

```powershell
uv run pytest
uv run python -c "import nexus_observer; print(nexus_observer.__name__)"
```
