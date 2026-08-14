"""AT22 (spec §61, §78, §97): capability events are events like any other.

``capability_missing`` and ``capability_available`` go through the Phase 3F
delivery boundary unchanged.  That matters most for availability: a system that
gained a competence and crashed before reconciling must still reconcile — the
alternative is work that stays blocked forever for no visible reason.
"""

from __future__ import annotations

from capability_helpers import (
    instances_named,
    make_work,
    offer_work,
    register_capable,
    status_of,
    work_pipeline,
)

from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


async def test_capability_events_get_delivery_obligations(tmp_path):
    runtime = work_pipeline(Runtime(tmp_path / "d.db"))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    for event_type in ("capability_missing", "capability_available"):
        event = runtime.event_store.by_type(event_type)[0]
        assert runtime.get_event_delivery(event.id).status.value == "DELIVERED"
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_an_unrouted_capability_available_survives_a_crash(tmp_path):
    """AT22: the competence arrived, the runtime died, the work still runs."""
    db_path = tmp_path / "d.db"

    runtime = work_pipeline(Runtime(db_path))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)
    assert status_of(runtime, work) is WorkStatus.BLOCKED_CAPABILITY

    # Registering appends the event; nothing drains before the crash.
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    available = runtime.event_store.by_type("capability_available")[0]
    assert runtime.get_event_delivery(available.id).status.value == "PENDING"
    runtime.close()

    runtime2 = work_pipeline(Runtime(db_path))
    register_capable(runtime2, "analyzer", ("analyze_resistance",))
    await runtime2.run_pending()

    assert runtime2.get_event_delivery(available.id).status.value == "DELIVERED"
    assert status_of(runtime2, work) is WorkStatus.SATISFIED
    assert len(instances_named(runtime2, "analyzer")) == 1
    runtime2.close()


async def test_a_capability_missing_event_is_not_lost_either(tmp_path):
    """The gap record is what a self-extending system would later read."""
    db_path = tmp_path / "d.db"

    runtime = work_pipeline(Runtime(db_path))
    work = make_work(runtime, work_key="w1", required=("z",))
    # Offer the work without draining, so nothing routes.
    from capability_helpers import work_required

    runtime.event_store.append(work_required(work))
    runtime.close()

    runtime2 = work_pipeline(Runtime(db_path))
    await runtime2.run_pending()

    assert status_of(runtime2, work) is WorkStatus.BLOCKED_CAPABILITY
    missing = runtime2.event_store.by_type("capability_missing")
    assert len(missing) == 1
    assert runtime2.get_event_delivery(missing[0].id).status.value == "DELIVERED"
    runtime2.close()


async def test_reconciliation_is_not_replay(tmp_path):
    """Invariant 52: no raw event is re-delivered, and no work is duplicated."""
    runtime = work_pipeline(Runtime(tmp_path / "d.db"))
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)

    delivered_before = {
        d.event_id for d in runtime.get_event_deliveries() if d.status.value == "DELIVERED"
    }

    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    delivered_after = {
        d.event_id for d in runtime.get_event_deliveries() if d.status.value == "DELIVERED"
    }
    # Everything delivered before is *still* delivered — none was re-attempted.
    assert delivered_before <= delivered_after
    for event_id in delivered_before:
        assert runtime.get_event_delivery(event_id).attempt_count == 1

    # And exactly one requirement exists, with its original id.
    assert [w.id for w in runtime.get_work_requirements()] == [work.id]
    runtime.close()


async def test_repeated_registration_does_not_churn_the_queue(tmp_path):
    """Re-registering announces nothing, so nothing is re-reconciled."""
    runtime = work_pipeline(Runtime(tmp_path / "d.db"))
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    for _ in range(3):
        register_capable(runtime, "analyzer", ("analyze_resistance",))
        await runtime.run_pending()

    assert len(runtime.event_store.by_type("capability_available")) == 1
    assert len(instances_named(runtime, "reconcile_blocked_work")) == 1
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()
