"""World-state history: every version is retained, versions increase."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


def _change(parameter, old, new):
    payload = {"parameter": parameter, "new": new, "unit": "nm"}
    if old is not None:
        payload["old"] = old
    return Event("process_parameter_changed", "world", payload)


async def test_history_retains_all_versions(tmp_path):
    runtime = Runtime(tmp_path / "hist.db")
    bootstrap_semantic(runtime)

    # None -> 48 -> 45 -> 43
    await runtime.submit_event(_change("D1_CD", None, 48))
    await runtime.submit_event(_change("D1_CD", 48, 45))
    await runtime.submit_event(_change("D1_CD", 45, 43))

    assert runtime.get_current_state("D1_CD", "target").value == 43

    history = runtime.get_state_history("D1_CD", "target")
    assert [h.value for h in history] == [48, 45, 43]
    assert [h.version for h in history] == [1, 2, 3]

    # Versions are monotonically increasing and only the latest is open.
    open_versions = [h for h in history if h.valid_to is None]
    assert len(open_versions) == 1
    assert open_versions[0].version == 3

    # Point-in-time access by version.
    assert runtime.get_state_at_version("D1_CD", "target", 1).value == 48
    assert runtime.get_state_at_version("D1_CD", "target", 2).value == 45
    runtime.close()
