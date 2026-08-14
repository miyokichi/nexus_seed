"""AT15 (spec §71): a need outlives the runtime that could not meet it.

Blocked work is durable state, not a note in memory.  A system that was
restarted while unable to do something must still know it owes that work when
the competence arrives.
"""

from __future__ import annotations

from capability_helpers import (
    RECORDED,
    instances_named,
    make_work,
    offer_work,
    register_capable,
    status_of,
    work_pipeline,
)

from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


async def test_blocked_work_survives_a_restart_and_runs_later(tmp_path):
    """AT15."""
    db_path = tmp_path / "r.db"

    runtime = work_pipeline(Runtime(db_path))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)
    assert status_of(runtime, work) is WorkStatus.BLOCKED_CAPABILITY
    runtime.close()

    # --- rebuilt; still blocked, and still remembers why ---
    runtime2 = work_pipeline(Runtime(db_path))
    stored = runtime2.work_requirement_store.get(work.id)
    assert stored.status is WorkStatus.BLOCKED_CAPABILITY
    assert stored.missing_capabilities == ["analyze_resistance"]
    assert [r.name for r in stored.required_capabilities] == ["analyze_resistance"]
    assert runtime2.get_blocked_capability_work()[0].id == work.id

    register_capable(runtime2, "resistance_analyzer", ("analyze_resistance",))
    await runtime2.run_pending()

    assert status_of(runtime2, work) is WorkStatus.SATISFIED
    assert len(instances_named(runtime2, "resistance_analyzer")) == 1
    runtime2.close()


async def test_required_capabilities_round_trip_through_storage(tmp_path):
    from nexus_seed.capabilities.models import CapabilityRequirement
    from nexus_seed.work.work_requirement import WorkRequirement

    db_path = tmp_path / "r.db"
    runtime = Runtime(db_path)
    requirement = WorkRequirement(
        work_type="demo",
        work_key="w1",
        required_capabilities=[
            CapabilityRequirement(name="a", version_constraint="2"),
            CapabilityRequirement(name="b", required=False, metadata={"why": "nice"}),
        ],
    )
    runtime.work_requirement_store.save(requirement)
    runtime.close()

    runtime2 = Runtime(db_path)
    stored = runtime2.work_requirement_store.get(requirement.id)
    assert [r.name for r in stored.required_capabilities] == ["a", "b"]
    assert stored.required_capabilities[0].version_constraint == "2"
    assert stored.required_capabilities[1].required is False
    assert stored.required_capabilities[1].metadata == {"why": "nice"}
    assert stored.needs_capabilities
    runtime2.close()


async def test_the_selected_definition_survives_a_restart(tmp_path):
    """Spawning must not have to re-decide after a rebuild."""
    db_path = tmp_path / "r.db"

    runtime = work_pipeline(Runtime(db_path))
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)
    runtime.close()

    runtime2 = Runtime(db_path)
    stored = runtime2.work_requirement_store.get(work.id)
    assert stored.selected_definition_name == "analyzer"
    assert stored.selected_definition_version == "1"
    runtime2.close()


async def test_the_capability_available_event_survives_a_crash(tmp_path):
    """AT22: registering a capability during a crashed startup is not lost.

    ``register_process`` only *appends* the event; Phase 3F's delivery
    obligation carries it.  So a runtime that registers a capability and dies
    before draining still reconciles on the next start.
    """
    db_path = tmp_path / "r.db"

    runtime = work_pipeline(Runtime(db_path))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)
    runtime.close()

    # Registers the competence, then dies before anything is routed.
    runtime2 = work_pipeline(Runtime(db_path))
    register_capable(runtime2, "analyzer", ("analyze_resistance",))
    available = runtime2.event_store.by_type("capability_available")[0]
    assert runtime2.get_event_delivery(available.id).status.value == "PENDING"
    assert status_of(runtime2, work) is WorkStatus.BLOCKED_CAPABILITY
    runtime2.close()

    # --- restart: the obligation is still owed, and reconciliation happens ---
    runtime3 = work_pipeline(Runtime(db_path))
    register_capable(runtime3, "analyzer", ("analyze_resistance",))
    RECORDED.clear()
    await runtime3.run_pending()

    assert runtime3.get_event_delivery(available.id).status.value == "DELIVERED"
    assert status_of(runtime3, work) is WorkStatus.SATISFIED
    assert len(instances_named(runtime3, "analyzer")) == 1
    runtime3.close()


async def test_a_restart_does_not_re_announce_and_re_reconcile(tmp_path):
    """Otherwise every startup would churn through all blocked work."""
    db_path = tmp_path / "r.db"

    runtime = work_pipeline(Runtime(db_path))
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()
    announced = len(runtime.event_store.by_type("capability_available"))
    runtime.close()

    runtime2 = work_pipeline(Runtime(db_path))
    register_capable(runtime2, "analyzer", ("analyze_resistance",))
    await runtime2.run_pending()

    assert len(runtime2.event_store.by_type("capability_available")) == announced
    assert len(instances_named(runtime2, "reconcile_blocked_work")) == 1
    runtime2.close()
