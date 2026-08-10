"""Work trace: a running process traces back to the raw event that caused it."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime


async def test_work_traces_to_raw_event(tmp_path):
    runtime = Runtime(tmp_path / "trace.db")
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)

    raw = Event(
        "process_parameter_changed",
        "world",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )
    await runtime.submit_event(raw)

    req = [
        r for r in runtime.get_work_requirements() if r.work_type == "resistance_check"
    ][0]

    trace = runtime.get_work_trace(req.id)
    assert trace is not None
    # ProcessInstance -> WorkRequirement -> StateDelta -> Observation -> Raw Event
    assert trace.process_instance is not None
    assert trace.process_instance.definition_name == "resistance_check"
    assert trace.requirement.id == req.id
    assert trace.state_delta is not None
    assert trace.state_delta.new_value == 45
    assert trace.observation is not None
    assert trace.observation.subject == "D1_CD"
    assert trace.source_event is not None
    assert trace.source_event.id == raw.id
    assert trace.source_event.type == "process_parameter_changed"
    runtime.close()
