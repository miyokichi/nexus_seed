"""Work idempotency: a re-delivered change never duplicates work.

Two ``state_changed`` events for the same entity/attribute/version (distinct
event ids) must yield exactly one WorkRequirement and one work process, thanks
to the version-bearing ``work_key``.
"""

from __future__ import annotations

import uuid

from nexus_seed.core.event import Event
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime


def _state_changed():
    return Event(
        "state_changed",
        "apply_state_delta",
        {
            "entity": "D1_CD",
            "attribute": "target",
            "old_value": 48,
            "new_value": 45,
            "version": 1,
            "state_delta_id": str(uuid.uuid4()),
        },
    )


async def test_duplicate_state_change_yields_single_work(tmp_path):
    runtime = Runtime(tmp_path / "workidem.db")
    bootstrap_work_intelligence(runtime)

    await runtime.submit_event(_state_changed())
    await runtime.submit_event(_state_changed())  # different id, same work_key

    resistance_reqs = [
        r for r in runtime.get_work_requirements() if r.work_type == "resistance_check"
    ]
    assert len(resistance_reqs) == 1

    processes = runtime.process_store.find_by_work_key("resistance_check:D1_CD:v1")
    assert len(processes) == 1
    runtime.close()


async def test_new_state_version_is_distinct_work(tmp_path):
    runtime = Runtime(tmp_path / "workver.db")
    bootstrap_work_intelligence(runtime)

    # v1 then v2 -> two distinct work items.
    v1 = _state_changed()
    v2 = Event(
        "state_changed",
        "apply_state_delta",
        {
            "entity": "D1_CD",
            "attribute": "target",
            "old_value": 45,
            "new_value": 43,
            "version": 2,
            "state_delta_id": str(uuid.uuid4()),
        },
    )
    await runtime.submit_event(v1)
    await runtime.submit_event(v2)

    keys = {
        r.work_key
        for r in runtime.get_work_requirements()
        if r.work_type == "resistance_check"
    }
    assert keys == {"resistance_check:D1_CD:v1", "resistance_check:D1_CD:v2"}
    runtime.close()
