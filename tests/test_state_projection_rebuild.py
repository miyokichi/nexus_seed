"""Projection rebuild: world_state_current can be rebuilt from history."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


def _change(old, new):
    payload = {"parameter": "D1_CD", "new": new}
    if old is not None:
        payload["old"] = old
    return Event("process_parameter_changed", "world", payload)


async def test_rebuild_current_from_history(tmp_path):
    runtime = Runtime(tmp_path / "rebuild.db")
    bootstrap_semantic(runtime)

    await runtime.submit_event(_change(None, 48))
    await runtime.submit_event(_change(48, 45))
    await runtime.submit_event(_change(45, 43))

    assert runtime.state_store.get("D1_CD", "target") == 43

    # Corrupt the projection: wipe world_state_current entirely.
    runtime.db.execute("DELETE FROM world_state_current")
    assert runtime.get_current_state("D1_CD", "target") is None

    # Rebuild it from the history (the source of truth).
    rebuilt = runtime.rebuild_current_state()
    assert rebuilt == 1

    current = runtime.get_current_state("D1_CD", "target")
    assert current is not None
    assert current.value == 43
    assert current.version == 3
    # History is untouched by the rebuild.
    assert [h.value for h in runtime.get_state_history("D1_CD", "target")] == [48, 45, 43]
    runtime.close()
