"""AT18 (spec §16, §17, §55, §74, §81): the old path and the new one coexist.

Migrating a matching mechanism is only safe if the existing behaviour is
provably unchanged.  The same scenario must reach the same process — what
differs is *how* it was chosen, not *what* was chosen.
"""

from __future__ import annotations

from capability_helpers import (
    capability_runtime,
    instances_named,
    make_work,
    offer_work,
    register_capable,
    status_of,
)

from nexus_seed.core.event import Event
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.rules import (
    WORK_CAPABILITIES,
    WORK_PROCESS_REGISTRY,
    capabilities_for_work_type,
    process_for_work_type,
)
from nexus_seed.work.work_requirement import WorkStatus


def parameter_change() -> Event:
    return Event(
        "process_parameter_changed",
        "fab",
        {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
    )


def test_the_rules_agree_about_resistance_work(tmp_path):
    """Both tables point at the same competence (spec §54, §55)."""
    assert capabilities_for_work_type("resistance_check") == ["analyze_resistance"]
    assert process_for_work_type("resistance_check") == ("resistance_check", "1")
    assert set(WORK_CAPABILITIES) == set(WORK_PROCESS_REGISTRY)


async def test_the_existing_scenario_reaches_the_same_process(tmp_path):
    """AT18: D1_CD target change still runs resistance_check."""
    runtime = Runtime(tmp_path / "legacy.db")
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)

    await runtime.submit_event(parameter_change())

    requirement = runtime.get_work_requirements()[0]
    assert requirement.work_type == "resistance_check"
    # ...but it got there by capability now.
    assert [r.name for r in requirement.required_capabilities] == ["analyze_resistance"]
    assert requirement.selected_definition_name == "resistance_check"
    assert len(instances_named(runtime, "resistance_check")) == 1
    runtime.close()


async def test_the_choice_is_recorded_where_the_old_path_recorded_nothing(tmp_path):
    runtime = Runtime(tmp_path / "legacy.db")
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    await runtime.submit_event(parameter_change())

    requirement = runtime.get_work_requirements()[0]
    trace = runtime.get_capability_trace(requirement.id)

    assert trace.selected == ("resistance_check", "1")
    assert trace.required_capabilities == ["analyze_resistance"]
    assert not trace.was_blocked
    runtime.close()


async def test_work_declaring_no_capabilities_uses_the_name_table(tmp_path):
    """Spec §16: the legacy path is still there for work that predates 4A."""
    runtime = capability_runtime(tmp_path)
    from nexus_seed.processes.work_intelligence import RESISTANCE_CHECK, resistance_check

    runtime.register_process(RESISTANCE_CHECK, resistance_check)

    work = make_work(
        runtime, work_key="legacy", work_type="resistance_check", required=()
    )
    await offer_work(runtime, work)

    # Spawned via WORK_PROCESS_REGISTRY, with no capability match recorded.
    assert len(instances_named(runtime, "resistance_check")) == 1
    assert runtime.get_capability_matches(work.id) == []
    runtime.close()


async def test_an_unknown_work_type_with_no_capabilities_is_cancelled(tmp_path):
    """The legacy behaviour for an unimplementable name is unchanged."""
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", work_type="nobody_knows", required=())

    await offer_work(runtime, work)

    # No capabilities declared -> legacy path -> no entry in the name table.
    assert status_of(runtime, work) is WorkStatus.CANCELLED
    runtime.close()


async def test_capability_work_blocks_where_legacy_work_cancels(tmp_path):
    """The behavioural difference that matters (Invariant 51).

    Same unimplementable work: the legacy path throws the need away, the
    capability path keeps it so a later competence can pick it up.
    """
    runtime = capability_runtime(tmp_path)
    legacy = make_work(runtime, work_key="legacy", work_type="unknown", required=())
    modern = make_work(runtime, work_key="modern", work_type="unknown", required=("z",))

    await offer_work(runtime, legacy)
    await offer_work(runtime, modern)

    assert status_of(runtime, legacy) is WorkStatus.CANCELLED
    assert status_of(runtime, modern) is WorkStatus.BLOCKED_CAPABILITY
    runtime.close()


async def test_a_selected_definition_overrides_the_name_table(tmp_path):
    """Capability matching is the primary mechanism (spec §17)."""
    runtime = capability_runtime(tmp_path)
    from nexus_seed.processes.work_intelligence import RESISTANCE_CHECK, resistance_check

    runtime.register_process(RESISTANCE_CHECK, resistance_check)
    # A different process also claims the competence, and bids higher.
    register_capable(runtime, "better_analyzer", ("analyze_resistance",), priority=10)

    work = make_work(
        runtime,
        work_key="w1",
        work_type="resistance_check",
        required=("analyze_resistance",),
    )
    await offer_work(runtime, work)

    # The name table would have said resistance_check; capability matching won.
    assert len(instances_named(runtime, "better_analyzer")) == 1
    assert instances_named(runtime, "resistance_check") == []
    runtime.close()


async def test_a_pre_4a_database_still_works(tmp_path):
    """Spec §81: existing requirements have no capabilities and keep running."""
    db_path = tmp_path / "old.db"

    runtime = capability_runtime(tmp_path, )
    runtime.close()

    # Simulate a requirement written before the columns existed.
    runtime = Runtime(db_path)
    runtime.db.execute(
        "INSERT INTO work_requirements (id, work_type, work_key, related_entities, "
        "reason, priority, status, metadata, created_at, updated_at) "
        "VALUES (?, ?, ?, '[]', '', 0, 'EXPECTED', '{}', ?, ?)",
        (
            "11111111-1111-1111-1111-111111111111",
            "resistance_check",
            "legacy-key",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )

    import uuid

    stored = runtime.work_requirement_store.get(
        uuid.UUID("11111111-1111-1111-1111-111111111111")
    )
    assert stored.required_capabilities == []
    assert stored.missing_capabilities == []
    assert not stored.needs_capabilities
    runtime.close()
