"""AT13 + AT14 + AT21 (spec §69, §70, §77): acquiring a competence later.

The reason blocked work is *held* rather than cancelled.  When a capability
arrives, the existing requirements are re-offered to the matcher — the same
requirement objects, with the same ids and the same provenance.

This is emphatically **not** event replay (Invariant 52 / spec §98).  Nothing
is re-interpreted, no raw event is re-read, and no second WorkRequirement is
created.  The need was already recorded; only our ability to meet it changed.
"""

from __future__ import annotations

from capability_helpers import (
    RECORDED,
    capability_runtime,
    instances_named,
    make_work,
    offer_work,
    register_capable,
    status_of,
)

from nexus_seed.capabilities.models import CapabilityMatchStatus
from nexus_seed.work.work_requirement import WorkStatus


async def test_blocked_work_runs_once_the_capability_arrives(tmp_path):
    """AT13."""
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))

    await offer_work(runtime, work)
    assert status_of(runtime, work) is WorkStatus.BLOCKED_CAPABILITY

    # The competence arrives.  Registering appends capability_available; the
    # drain routes it to the reconciler.
    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    assert status_of(runtime, work) is WorkStatus.SATISFIED
    assert len(instances_named(runtime, "resistance_analyzer")) == 1
    assert RECORDED[0]["work_key"] == "w1"
    runtime.close()


async def test_reconciliation_reuses_the_same_requirement(tmp_path):
    """AT14: no duplicate need, no replayed event."""
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)

    events_before = len(runtime.event_store.all())
    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    requirements = runtime.get_work_requirements()
    assert len(requirements) == 1
    assert requirements[0].id == work.id  # the very same requirement
    assert requirements[0].created_at == work.created_at
    # New events were emitted, but no raw event was re-delivered.
    assert len(runtime.event_store.all()) > events_before
    assert len(runtime.event_store.by_type("work_required")) == 2  # offer + reconcile
    runtime.close()


async def test_only_relevant_work_is_reconsidered(tmp_path):
    """Spec §87: a requirement blocked on something else is left alone."""
    runtime = capability_runtime(tmp_path)
    resistance = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    summary = make_work(runtime, work_key="w2", required=("summarize_report",))
    await offer_work(runtime, resistance)
    await offer_work(runtime, summary)

    register_capable(runtime, "resistance_analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    assert status_of(runtime, resistance) is WorkStatus.SATISFIED
    assert status_of(runtime, summary) is WorkStatus.BLOCKED_CAPABILITY
    runtime.close()


async def test_enabling_a_capability_also_unblocks_work(tmp_path):
    """AT21 / spec §84: regaining a competence is new availability."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    runtime.set_capability_enabled("analyze_resistance", "1", False)
    await runtime.run_pending()

    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)
    assert status_of(runtime, work) is WorkStatus.BLOCKED_CAPABILITY

    runtime.set_capability_enabled("analyze_resistance", "1", True)
    await runtime.run_pending()

    assert status_of(runtime, work) is WorkStatus.SATISFIED
    assert len(instances_named(runtime, "analyzer")) == 1
    runtime.close()


async def test_composition_required_work_unblocks_when_one_process_covers_all(tmp_path):
    """The Phase 4B boundary, approached from the Phase 4A side."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "p1", ("a",))
    register_capable(runtime, "p2", ("b",))
    work = make_work(runtime, work_key="w1", required=("a", "b"))

    await offer_work(runtime, work)
    assert status_of(runtime, work) is WorkStatus.BLOCKED_CAPABILITY
    assert (
        runtime.get_capability_matches(work.id)[0].status
        is CapabilityMatchStatus.COMPOSITION_REQUIRED
    )

    # Not by combining p1 and p2 — by a process that can do both.
    register_capable(runtime, "combined", ("a", "b"))
    await runtime.run_pending()

    assert status_of(runtime, work) is WorkStatus.SATISFIED
    assert len(instances_named(runtime, "combined")) == 1
    assert instances_named(runtime, "p1") == []
    assert instances_named(runtime, "p2") == []
    runtime.close()


async def test_reconciliation_is_a_process_not_a_runtime_feature(tmp_path):
    """Invariant 50: the Runtime just routes capability_available."""
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)

    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    reconcilers = instances_named(runtime, "reconcile_blocked_work")
    assert len(reconcilers) == 1
    assert reconcilers[0].local_state["output"]["reopened"] == ["w1"]
    assert reconcilers[0].local_state["output"]["capability"] == "analyze_resistance"
    runtime.close()


async def test_nothing_blocked_means_nothing_to_reconcile(tmp_path):
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    reconcilers = instances_named(runtime, "reconcile_blocked_work")
    assert len(reconcilers) == 1
    assert reconcilers[0].local_state["output"]["reopened"] == []
    assert runtime.get_work_requirements() == []
    runtime.close()


async def test_several_blocked_requirements_unblock_together(tmp_path):
    runtime = capability_runtime(tmp_path)
    works = [
        make_work(runtime, work_key=f"w{n}", required=("analyze_resistance",))
        for n in range(3)
    ]
    for work in works:
        await offer_work(runtime, work)
    assert all(status_of(runtime, w) is WorkStatus.BLOCKED_CAPABILITY for w in works)

    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    assert all(status_of(runtime, w) is WorkStatus.SATISFIED for w in works)
    assert len(instances_named(runtime, "analyzer")) == 3
    runtime.close()


async def test_both_matching_attempts_are_kept(tmp_path):
    """AT16 / spec §48: the history explains the delay."""
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("analyze_resistance",))
    await offer_work(runtime, work)

    register_capable(runtime, "analyzer", ("analyze_resistance",))
    await runtime.run_pending()

    attempts = runtime.get_capability_matches(work.id)
    assert [a.status for a in attempts] == [
        CapabilityMatchStatus.MISSING_CAPABILITY,
        CapabilityMatchStatus.MATCHED_SINGLE_PROCESS,
    ]
    assert attempts[0].missing_capabilities == ["analyze_resistance"]
    assert attempts[1].selected_definition_name == "analyzer"
    runtime.close()
