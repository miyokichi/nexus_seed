"""Work context: a spawned work process sees its WorkRequirement via context."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime


async def test_resistance_check_has_work_requirement_in_context(tmp_path):
    runtime = Runtime(tmp_path / "work.db")
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)

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
    definition = runtime.process_store.get_definition("resistance_check", "1")

    view = runtime.context_compiler.compile(
        definition=definition,
        process_instance=instance,
        continuation=runtime.continuation_store.for_instance(instance.id),
    )

    # The WorkRequirement is compiled in — no direct WorkStore query needed.
    assert view.current_work_requirement is not None
    assert view.current_work_requirement.id == instance.work_requirement_id
    assert view.current_work_requirement.work_type == "resistance_check"
    # include_work_entities pulled D1_CD into world state.
    assert view.get_state("D1_CD", "target") == 45
    runtime.close()
