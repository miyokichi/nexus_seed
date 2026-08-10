"""interpret_event produces and persists an Observation (distinct from a delta)."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


async def test_interpret_event_creates_observation(tmp_path):
    runtime = Runtime(tmp_path / "obs.db")
    bootstrap_semantic(runtime)

    event = Event(
        "process_parameter_changed",
        "world",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )
    await runtime.submit_event(event)

    observations = runtime.observation_store.all()
    assert len(observations) == 1
    obs = observations[0]
    assert obs.subject == "D1_CD"
    assert obs.predicate == "target_changed"
    assert obs.extracted == {"old": 48, "new": 45, "unit": "nm"}
    assert obs.confidence == 1.0
    assert obs.source_event_id == event.id
    assert obs.created_by_process_id is not None
    runtime.close()


async def test_observation_persists_across_restart(tmp_path):
    db_path = tmp_path / "obs.db"
    runtime = Runtime(db_path)
    bootstrap_semantic(runtime)
    event = Event(
        "process_parameter_changed",
        "world",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )
    await runtime.submit_event(event)
    obs_id = runtime.observation_store.all()[0].id
    runtime.close()

    runtime2 = Runtime(db_path)
    fetched = runtime2.observation_store.get(obs_id)
    assert fetched is not None
    assert fetched.subject == "D1_CD"
    runtime2.close()
