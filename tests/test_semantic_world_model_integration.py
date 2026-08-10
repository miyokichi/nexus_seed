"""Phase 2B integration: raw event -> observation -> delta -> state, across restart.

Exercises the full semantic pipeline and then rebuilds the runtime from SQLite
to prove history, current and provenance all survive.
"""

from __future__ import annotations

import uuid

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


async def test_full_semantic_pipeline_with_restart(tmp_path):
    db_path = tmp_path / "semantic.db"
    correlation = uuid.uuid4()

    # --- Runtime #1: run the pipeline end to end. ---
    runtime = Runtime(db_path)
    bootstrap_semantic(runtime)

    raw = Event(
        "process_parameter_changed",
        "world",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        correlation_id=correlation,
    )
    produced = await runtime.submit_event(raw)

    produced_types = [e.type for e in produced]
    assert "state_delta_created" in produced_types
    assert "state_changed" in produced_types

    # Observation + StateDelta persisted; world state updated.
    assert len(runtime.observation_store.all()) == 1
    assert len(runtime.state_delta_store.all()) == 1
    assert runtime.state_store.get("D1_CD", "target") == 45

    # interpret_event and apply_state_delta both completed.
    by_name = {i.definition_name: i for i in runtime.process_store.all_instances()}
    assert by_name["interpret_event"].status is ProcessStatus.COMPLETED
    assert by_name["apply_state_delta"].status is ProcessStatus.COMPLETED

    runtime.close()

    # --- Runtime #2: rebuilt from SQLite only. ---
    runtime2 = Runtime(db_path)

    # Current state recovered.
    assert runtime2.state_store.get("D1_CD", "target") == 45
    # History recovered.
    history = runtime2.get_state_history("D1_CD", "target")
    assert [h.value for h in history] == [45]
    # Provenance recovered all the way to the raw event.
    prov = runtime2.get_state_provenance("D1_CD", "target")
    assert prov.source_event.id == raw.id
    assert prov.state_delta.new_value == 45
    assert prov.observation.subject == "D1_CD"

    # The current projection is still rebuildable from history after restart.
    runtime2.db.execute("DELETE FROM world_state_current")
    assert runtime2.rebuild_current_state() == 1
    assert runtime2.state_store.get("D1_CD", "target") == 45
    runtime2.close()
