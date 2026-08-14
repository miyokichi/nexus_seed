"""AT18 through the real pipeline (spec §74): impact → capability → process.

The existing Phase 2C flow, now routed through the registry.  Nothing in impact
analysis, spawning or the work process itself had to learn about capabilities;
only what a requirement *says about itself* changed.
"""

from __future__ import annotations

from capability_helpers import instances_named, register_capable

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


def change(new: int = 45, old: int = 48) -> Event:
    return Event(
        "process_parameter_changed",
        "fab",
        {"parameter": "D1_CD", "old": old, "new": new, "unit": "nm"},
    )


def full(runtime):
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    return runtime


async def test_impact_analysis_declares_what_the_work_takes(tmp_path):
    runtime = full(Runtime(tmp_path / "i.db"))

    await runtime.submit_event(change())

    requirement = runtime.get_work_requirements()[0]
    assert requirement.work_type == "resistance_check"
    assert [r.name for r in requirement.required_capabilities] == ["analyze_resistance"]
    assert requirement.needs_capabilities
    runtime.close()


async def test_the_whole_flow_runs_on_capability_matching(tmp_path):
    """AT18."""
    runtime = full(Runtime(tmp_path / "i.db"))

    await runtime.submit_event(change())

    assert runtime.state_store.get("D1_CD", "target") == 45
    requirement = runtime.get_work_requirements()[0]
    assert requirement.selected_definition_name == "resistance_check"

    worker = instances_named(runtime, "resistance_check")[0]
    assert worker.status is ProcessStatus.SUSPENDED  # waiting for a measurement
    assert requirement.status is WorkStatus.SPAWNED

    await runtime.submit_event(
        Event("measurement_completed", "metrology", {"wafer": "W03", "resistance": 123.4})
    )

    assert runtime.process_store.get_instance(worker.id).status is ProcessStatus.COMPLETED
    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    runtime.close()


async def test_without_the_capable_process_the_work_waits(tmp_path):
    """Register the pipeline but not the analyzer: the need is held."""
    runtime = Runtime(tmp_path / "i.db")
    bootstrap_semantic(runtime)

    from nexus_seed.processes.work_intelligence import (
        IMPACT_ANALYSIS,
        MISSING_WORK_DETECTOR,
        RECONCILE_BLOCKED_WORK,
        WORK_MATCHER,
        WORK_SPAWNER,
        impact_analysis,
        missing_work_detector,
        reconcile_blocked_work,
        work_matcher,
        work_spawner,
    )

    runtime.register_process(IMPACT_ANALYSIS, impact_analysis)
    runtime.register_process(WORK_MATCHER, work_matcher)
    runtime.register_process(MISSING_WORK_DETECTOR, missing_work_detector)
    runtime.register_process(WORK_SPAWNER, work_spawner)
    runtime.register_process(RECONCILE_BLOCKED_WORK, reconcile_blocked_work)

    await runtime.submit_event(change())

    assert runtime.state_store.get("D1_CD", "target") == 45  # the world still moved
    requirement = runtime.get_work_requirements()[0]
    assert requirement.status is WorkStatus.BLOCKED_CAPABILITY
    assert requirement.missing_capabilities == ["analyze_resistance"]
    assert runtime.event_store.by_type("capability_missing") != []
    runtime.close()


async def test_registering_the_analyzer_later_completes_the_work(tmp_path):
    """The whole point of holding the need."""
    runtime = Runtime(tmp_path / "i.db")
    bootstrap_semantic(runtime)

    from nexus_seed.processes.work_intelligence import (
        IMPACT_ANALYSIS,
        MISSING_WORK_DETECTOR,
        RECONCILE_BLOCKED_WORK,
        WORK_MATCHER,
        WORK_SPAWNER,
        impact_analysis,
        missing_work_detector,
        reconcile_blocked_work,
        work_matcher,
        work_spawner,
    )

    for definition, handler in (
        (IMPACT_ANALYSIS, impact_analysis),
        (WORK_MATCHER, work_matcher),
        (MISSING_WORK_DETECTOR, missing_work_detector),
        (WORK_SPAWNER, work_spawner),
        (RECONCILE_BLOCKED_WORK, reconcile_blocked_work),
    ):
        runtime.register_process(definition, handler)

    await runtime.submit_event(change())
    requirement = runtime.get_work_requirements()[0]
    assert requirement.status is WorkStatus.BLOCKED_CAPABILITY

    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    assert len(instances_named(runtime, "resistance_analyzer")) == 1
    # Still one requirement, with its original identity.
    assert len(runtime.get_work_requirements()) == 1
    runtime.close()


async def test_a_new_state_version_creates_new_work_with_the_same_needs(tmp_path):
    """work_key idempotency and capability matching are independent layers."""
    runtime = full(Runtime(tmp_path / "i.db"))

    await runtime.submit_event(change(45))
    await runtime.submit_event(
        Event("measurement_completed", "metrology", {"wafer": "W03", "resistance": 1.0})
    )
    await runtime.submit_event(change(42, old=45))

    requirements = runtime.get_work_requirements()
    assert len(requirements) == 2
    assert all(
        [r.name for r in w.required_capabilities] == ["analyze_resistance"]
        for w in requirements
    )
    assert all(w.selected_definition_name == "resistance_check" for w in requirements)
    runtime.close()
