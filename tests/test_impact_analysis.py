"""impact_analysis derives Expected Work (a WorkRequirement) from a change."""

from __future__ import annotations

import uuid

from nexus_seed.core.event import Event
from nexus_seed.processes.work_intelligence import IMPACT_ANALYSIS, impact_analysis
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


async def test_state_changed_produces_resistance_check_requirement(tmp_path):
    runtime = Runtime(tmp_path / "impact.db")
    # Register only impact_analysis to isolate this stage.
    runtime.register_process(IMPACT_ANALYSIS, impact_analysis)

    produced = await runtime.submit_event(
        Event(
            "state_changed",
            "apply_state_delta",
            {
                "entity": "D1_CD",
                "attribute": "target",
                "old_value": 48,
                "new_value": 45,
                "version": 2,
                "state_delta_id": str(uuid.uuid4()),
            },
        )
    )

    requirements = runtime.get_work_requirements()
    assert len(requirements) == 1
    req = requirements[0]
    assert req.work_type == "resistance_check"
    assert req.work_key == "resistance_check:D1_CD:v2"
    assert req.related_entities == ["D1_CD"]
    assert req.status is WorkStatus.EXPECTED
    assert req.source_state_delta_id is not None

    # It announces the need but does NOT spawn anything itself.
    assert "work_required" in [e.type for e in produced]
    assert runtime.process_store.find_by_work_key(req.work_key) == []
    runtime.close()


async def test_non_target_change_produces_no_work(tmp_path):
    runtime = Runtime(tmp_path / "impact2.db")
    runtime.register_process(IMPACT_ANALYSIS, impact_analysis)

    await runtime.submit_event(
        Event(
            "state_changed",
            "apply_state_delta",
            {"entity": "D1_CD", "attribute": "color", "new_value": "blue", "version": 1},
        )
    )
    assert runtime.get_work_requirements() == []
    runtime.close()
