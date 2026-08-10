"""Phase 2C integration: world change -> derive work -> spawn -> resume -> satisfy.

Runs the whole loop, including a runtime restart between suspend and resume, to
prove everything is recovered from SQLite.
"""

from __future__ import annotations

import uuid

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


def _bootstrap(runtime: Runtime) -> None:
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)


async def test_end_to_end_work_intelligence_with_restart(tmp_path):
    db_path = tmp_path / "wi.db"
    correlation = uuid.uuid4()

    # --- Runtime #1: change the world, derive + spawn work, suspend. ---
    runtime = Runtime(db_path)
    _bootstrap(runtime)

    raw = Event(
        "process_parameter_changed",
        "world",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        correlation_id=correlation,
    )
    produced = await runtime.submit_event(raw)
    produced_types = [e.type for e in produced]

    # Whole pipeline fired.
    for expected in (
        "state_delta_created",
        "state_changed",
        "work_required",
        "work_matched",
        "work_missing",
        "work_spawned",
    ):
        assert expected in produced_types, expected

    # World state updated by the semantic layer.
    assert runtime.state_store.get("D1_CD", "target") == 45

    # A resistance_check requirement exists and was SPAWNED.
    req = [
        r for r in runtime.get_work_requirements() if r.work_type == "resistance_check"
    ][0]
    assert req.status is WorkStatus.SPAWNED

    # The spawned resistance_check process is SUSPENDED (no W03 measurement yet).
    proc = runtime.process_store.find_by_work_requirement_id(req.id)[0]
    assert proc.status is ProcessStatus.SUSPENDED

    runtime.close()

    # --- Runtime #2: rebuilt from SQLite; the measurement arrives. ---
    runtime2 = Runtime(db_path)
    _bootstrap(runtime2)

    assert runtime2.process_store.get_instance(proc.id).status is ProcessStatus.SUSPENDED
    assert runtime2.get_work_requirement(req.id).status is WorkStatus.SPAWNED

    produced2 = await runtime2.submit_event(
        Event(
            "measurement_completed",
            "metrology",
            {"wafer": "W03", "resistance": 123.4},
            correlation_id=correlation,
        )
    )

    # Process resumed and completed.
    final = runtime2.process_store.get_instance(proc.id)
    assert final.status is ProcessStatus.COMPLETED
    assert "resistance_analysis_completed" in [e.type for e in produced2]

    # WorkRequirement is now SATISFIED.
    assert runtime2.get_work_requirement(req.id).status is WorkStatus.SATISFIED

    # Full work trace resolves back to the raw event.
    trace = runtime2.get_work_trace(req.id)
    assert trace.process_instance.id == proc.id
    assert trace.state_delta.new_value == 45
    assert trace.observation.subject == "D1_CD"
    assert trace.source_event.id == raw.id
    runtime2.close()


async def test_duplicate_state_changed_no_duplicate_work(tmp_path):
    """Same change delivered twice -> one requirement, one process (spec §30)."""
    runtime = Runtime(tmp_path / "wi_dup.db")
    _bootstrap(runtime)

    # Two state_changed events for the same version, distinct ids.
    def state_changed():
        return Event(
            "state_changed",
            "apply_state_delta",
            {
                "entity": "D1_CD",
                "attribute": "target",
                "old_value": 48,
                "new_value": 45,
                "version": 7,
                "state_delta_id": str(uuid.uuid4()),
            },
        )

    await runtime.submit_event(state_changed())
    await runtime.submit_event(state_changed())

    reqs = [r for r in runtime.get_work_requirements() if r.work_type == "resistance_check"]
    assert len(reqs) == 1
    assert len(runtime.process_store.find_by_work_key("resistance_check:D1_CD:v7")) == 1
    runtime.close()
