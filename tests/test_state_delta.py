"""interpret_event proposes a StateDelta; apply_state_delta applies it."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


async def test_delta_created_and_applied(tmp_path):
    runtime = Runtime(tmp_path / "delta.db")
    bootstrap_semantic(runtime)

    await runtime.submit_event(
        Event(
            "process_parameter_changed",
            "world",
            {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        )
    )

    # A delta was persisted...
    deltas = runtime.state_delta_store.all()
    assert len(deltas) == 1
    delta = deltas[0]
    assert (delta.entity, delta.attribute) == ("D1_CD", "target")
    assert delta.old_value == 48
    assert delta.new_value == 45
    assert delta.observation_id == runtime.observation_store.all()[0].id

    # ...and applied to world state.
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()


async def test_state_changed_event_emitted(tmp_path):
    runtime = Runtime(tmp_path / "delta.db")
    bootstrap_semantic(runtime)

    produced = await runtime.submit_event(
        Event(
            "process_parameter_changed",
            "world",
            {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        )
    )

    types = [e.type for e in produced]
    assert "state_delta_created" in types
    assert "state_changed" in types

    changed = [e for e in produced if e.type == "state_changed"][0]
    assert changed.payload["entity"] == "D1_CD"
    assert changed.payload["new_value"] == 45
    assert changed.payload["version"] == 1
    runtime.close()
