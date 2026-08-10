"""work_matcher: NEW when nothing exists, ALREADY_RUNNING when it does."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessInstance, ProcessStatus
from nexus_seed.processes.work_intelligence import WORK_MATCHER, work_matcher
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


def _requirement() -> WorkRequirement:
    return WorkRequirement(
        work_type="resistance_check",
        work_key="resistance_check:D1_CD:v1",
        related_entities=["D1_CD"],
    )


async def test_new_when_no_existing_process(tmp_path):
    runtime = Runtime(tmp_path / "match.db")
    runtime.register_process(WORK_MATCHER, work_matcher)
    req = _requirement()
    runtime.work_requirement_store.save(req)

    produced = await runtime.submit_event(
        Event("work_required", "impact_analysis", {"work_requirement_id": str(req.id)})
    )

    matched = [e for e in produced if e.type == "work_matched"][0]
    assert matched.payload["match_status"] == "NEW"
    # NEW leaves the requirement EXPECTED (nothing covers it yet).
    assert runtime.get_work_requirement(req.id).status is WorkStatus.EXPECTED
    runtime.close()


async def test_already_running_when_active_process_exists(tmp_path):
    runtime = Runtime(tmp_path / "match2.db")
    runtime.register_process(WORK_MATCHER, work_matcher)
    req = _requirement()
    runtime.work_requirement_store.save(req)

    # A process already fulfilling this work_key is SUSPENDED (still active).
    existing = ProcessInstance(
        definition_name="resistance_check",
        definition_version="1",
        status=ProcessStatus.SUSPENDED,
        work_key=req.work_key,
        work_requirement_id=req.id,
    )
    runtime.process_store.save_instance(existing)

    produced = await runtime.submit_event(
        Event("work_required", "impact_analysis", {"work_requirement_id": str(req.id)})
    )

    matched = [e for e in produced if e.type == "work_matched"][0]
    assert matched.payload["match_status"] == "ALREADY_RUNNING"
    assert matched.payload["process_instance_id"] == str(existing.id)
    assert runtime.get_work_requirement(req.id).status is WorkStatus.MATCHED
    runtime.close()
