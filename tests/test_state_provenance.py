"""Provenance: a current fact can be traced back to the raw event."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


async def test_current_value_traces_to_raw_event(tmp_path):
    runtime = Runtime(tmp_path / "prov.db")
    bootstrap_semantic(runtime)

    raw = Event(
        "process_parameter_changed",
        "world",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )
    await runtime.submit_event(raw)

    assert runtime.state_store.get("D1_CD", "target") == 45

    prov = runtime.get_state_provenance("D1_CD", "target")
    assert prov is not None
    # Current -> history -> delta -> observation -> raw event.
    assert prov.current.value == 45
    assert prov.history_entry is not None
    assert prov.history_entry.version == 1
    assert prov.state_delta is not None
    assert prov.state_delta.old_value == 48
    assert prov.state_delta.new_value == 45
    assert prov.observation is not None
    assert prov.observation.predicate == "target_changed"
    assert prov.source_event is not None
    assert prov.source_event.id == raw.id
    assert prov.source_event.type == "process_parameter_changed"
    runtime.close()


async def test_provenance_survives_restart(tmp_path):
    db_path = tmp_path / "prov.db"
    runtime = Runtime(db_path)
    bootstrap_semantic(runtime)
    raw = Event(
        "process_parameter_changed",
        "world",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )
    await runtime.submit_event(raw)
    runtime.close()

    runtime2 = Runtime(db_path)
    prov = runtime2.get_state_provenance("D1_CD", "target")
    assert prov.source_event.id == raw.id
    assert prov.observation.subject == "D1_CD"
    runtime2.close()
