"""missing_work_detector: spawn required only when the match is NEW."""

from __future__ import annotations

import uuid

from nexus_seed.core.event import Event
from nexus_seed.processes.work_intelligence import (
    MISSING_WORK_DETECTOR,
    missing_work_detector,
)
from nexus_seed.runtime.runtime import Runtime


async def test_new_match_requires_spawn(tmp_path):
    runtime = Runtime(tmp_path / "missing.db")
    runtime.register_process(MISSING_WORK_DETECTOR, missing_work_detector)

    produced = await runtime.submit_event(
        Event(
            "work_matched",
            "work_matcher",
            {"work_requirement_id": str(uuid.uuid4()), "match_status": "NEW"},
        )
    )
    assert "work_missing" in [e.type for e in produced]
    runtime.close()


async def test_already_running_needs_no_spawn(tmp_path):
    runtime = Runtime(tmp_path / "missing2.db")
    runtime.register_process(MISSING_WORK_DETECTOR, missing_work_detector)

    produced = await runtime.submit_event(
        Event(
            "work_matched",
            "work_matcher",
            {"work_requirement_id": str(uuid.uuid4()), "match_status": "ALREADY_RUNNING"},
        )
    )
    assert "work_missing" not in [e.type for e in produced]
    runtime.close()
