"""Phase 3A integration: full pipeline, restart, fresh-resume context.

Covers Acceptance Test 9 (runtime restart restores ContextRequirements from the
DB and recompiles a fresh context) and the Phase 3A integration scenario (§44):
a suspended work process, after the world changes and the runtime restarts,
resumes seeing the *current* world.
"""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


def _bootstrap(runtime: Runtime) -> None:
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)


async def test_restart_recompiles_fresh_context(tmp_path):
    db_path = tmp_path / "ctx_restart.db"

    # --- Runtime #1: world change -> work -> spawn -> suspend. ---
    runtime = Runtime(db_path)
    _bootstrap(runtime)
    await runtime.submit_event(
        Event(
            "process_parameter_changed",
            "world",
            {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        )
    )

    instance = [
        i
        for i in runtime.process_store.all_instances()
        if i.definition_name == "resistance_check"
    ][0]
    assert instance.status is ProcessStatus.SUSPENDED
    req = runtime.get_work_requirement(instance.work_requirement_id)
    assert req.status is WorkStatus.SPAWNED

    # First activation compiled D1_CD.target == 45.
    snap = runtime.get_latest_context_snapshot(instance.id)
    assert snap.context_json["world_state"]["D1_CD"]["target"]["value"] == 45

    # The world changes while suspended.
    runtime.state_store.set("D1_CD", "target", 43)
    runtime.close()

    # --- Runtime #2: rebuilt from SQLite. ---
    runtime2 = Runtime(db_path)

    # ContextRequirements were restored from the DB (before any re-register).
    definition = runtime2.process_store.get_definition("resistance_check", "1")
    assert definition.context_requirements is not None
    assert definition.context_requirements.world_state.include_work_entities is True

    _bootstrap(runtime2)
    assert runtime2.process_store.get_instance(instance.id).status is ProcessStatus.SUSPENDED

    # The measurement arrives; the process resumes with a freshly compiled context.
    produced = await runtime2.submit_event(
        Event("measurement_completed", "metrology", {"wafer": "W03", "resistance": 123.4})
    )

    final = runtime2.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.COMPLETED
    # Resume saw the CURRENT target (43), not the suspend-time 45.
    assert final.local_state["output"]["observed_target"] == 43
    assert "resistance_analysis_completed" in [e.type for e in produced]

    # Work requirement satisfied; trace still resolves to the raw event.
    assert runtime2.get_work_requirement(req.id).status is WorkStatus.SATISFIED
    trace = runtime2.get_work_trace(req.id)
    assert trace.source_event.type == "process_parameter_changed"

    # The resume activation's snapshot recorded the fresh (43) context.
    resume_snap = runtime2.get_latest_context_snapshot(instance.id)
    assert resume_snap.context_json["world_state"]["D1_CD"]["target"]["value"] == 43
    runtime2.close()
