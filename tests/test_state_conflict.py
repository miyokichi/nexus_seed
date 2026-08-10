"""Conflict: a delta whose old_value disagrees with current is not applied."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


async def test_conflicting_delta_is_rejected(tmp_path):
    runtime = Runtime(tmp_path / "conflict.db")
    bootstrap_semantic(runtime)

    # Establish current D1_CD.target = 45 (None -> 45).
    await runtime.submit_event(
        Event("process_parameter_changed", "world", {"parameter": "D1_CD", "new": 45})
    )
    assert runtime.state_store.get("D1_CD", "target") == 45

    # Now apply a delta that expects old=48 (but current is 45) -> conflict.
    await runtime.submit_event(
        Event(
            "process_parameter_changed",
            "world",
            {"parameter": "D1_CD", "old": 48, "new": 43},
        )
    )

    # Current state is unchanged, and 43 never entered history.
    assert runtime.state_store.get("D1_CD", "target") == 45
    history_values = [h.value for h in runtime.get_state_history("D1_CD", "target")]
    assert history_values == [45]
    assert 43 not in history_values

    # The apply process ended FAILED (a domain StateConflict), no partial update.
    apply_instances = [
        i
        for i in runtime.process_store.all_instances()
        if i.definition_name == "apply_state_delta"
    ]
    statuses = {i.status for i in apply_instances}
    assert ProcessStatus.FAILED in statuses
    runtime.close()
