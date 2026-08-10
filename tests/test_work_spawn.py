"""work_spawner: spawn the mapped process and mark the requirement SPAWNED."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.work_intelligence import (
    RESISTANCE_CHECK,
    WORK_SPAWNER,
    resistance_check,
    work_spawner,
)
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


async def test_missing_work_spawns_process(tmp_path):
    runtime = Runtime(tmp_path / "spawn.db")
    runtime.register_process(WORK_SPAWNER, work_spawner)
    runtime.register_process(RESISTANCE_CHECK, resistance_check)

    req = WorkRequirement(
        work_type="resistance_check",
        work_key="resistance_check:D1_CD:v1",
        related_entities=["D1_CD"],
        priority=80,
    )
    runtime.work_requirement_store.save(req)

    produced = await runtime.submit_event(
        Event("work_missing", "missing_work_detector", {"work_requirement_id": str(req.id)})
    )

    # A resistance_check process was spawned, linked to the requirement.
    procs = runtime.process_store.find_by_work_key(req.work_key)
    assert len(procs) == 1
    proc = procs[0]
    assert proc.definition_name == "resistance_check"
    assert proc.work_requirement_id == req.id
    assert proc.priority == 80
    # It ran and suspended (no W03 measurement yet).
    assert proc.status is ProcessStatus.SUSPENDED

    # The requirement is now SPAWNED, and work_spawned was emitted.
    assert runtime.get_work_requirement(req.id).status is WorkStatus.SPAWNED
    assert "work_spawned" in [e.type for e in produced]
    runtime.close()


async def test_unknown_work_type_is_cancelled(tmp_path):
    runtime = Runtime(tmp_path / "spawn2.db")
    runtime.register_process(WORK_SPAWNER, work_spawner)

    req = WorkRequirement(work_type="unknown_work", work_key="unknown_work:X:v1")
    runtime.work_requirement_store.save(req)

    await runtime.submit_event(
        Event("work_missing", "missing_work_detector", {"work_requirement_id": str(req.id)})
    )
    assert runtime.get_work_requirement(req.id).status is WorkStatus.CANCELLED
    assert runtime.process_store.find_by_work_key(req.work_key) == []
    runtime.close()
