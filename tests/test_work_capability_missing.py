"""AT4 + AT5 + AT11 + AT12 (spec §60–§62, §67, §68): a need without a means.

The most important behaviour in this phase.  When the system cannot do
something, the *need does not go away* — it is held, with a record of what was
missing (Invariant 51).  Cancelling it would throw away a real requirement
because of a temporary limitation of our own, and acquiring the capability
later could never revive it.
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

from nexus_seed.capabilities.models import CapabilityMatchStatus
from nexus_seed.work.work_requirement import WorkStatus


async def test_work_the_system_cannot_do_is_blocked_not_cancelled(tmp_path):
    """AT4."""
    runtime = capability_runtime(tmp_path)
    work = make_work(
        runtime, work_key="w1", required=("summarize_semiconductor_report",)
    )

    await offer_work(runtime, work)

    stored = runtime.work_requirement_store.get(work.id)
    assert stored.status is WorkStatus.BLOCKED_CAPABILITY
    assert stored.status is not WorkStatus.CANCELLED
    assert stored.missing_capabilities == ["summarize_semiconductor_report"]
    assert runtime.process_store.all_instances() != []  # the pipeline ran
    assert instances_named(runtime, "summarizer") == []  # but nothing was spawned
    runtime.close()


async def test_a_capability_missing_event_is_emitted_once(tmp_path):
    """AT5, and it is a durable delivery like any other event."""
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("summarize_report",))

    await offer_work(runtime, work)

    events = runtime.event_store.by_type("capability_missing")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["work_requirement_id"] == str(work.id)
    assert payload["missing_capabilities"] == ["summarize_report"]
    assert payload["match_status"] == "MISSING_CAPABILITY"
    assert runtime.get_event_delivery(events[0].id).status.value == "DELIVERED"
    runtime.close()


async def test_a_capability_gap_is_not_a_runtime_failure(tmp_path):
    """Spec §34: ordinary domain state, not an error."""
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("summarize_report",))

    await offer_work(runtime, work)

    from nexus_seed.core.process import ProcessStatus

    assert all(
        i.status is ProcessStatus.COMPLETED for i in runtime.process_store.all_instances()
    )
    assert runtime.get_failed_event_deliveries() == []
    runtime.close()


async def test_partial_coverage_blocks_as_composition_required(tmp_path):
    """AT6 at the work level: every capability exists, none can do it alone."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "p1", ("a",))
    register_capable(runtime, "p2", ("b",))
    work = make_work(runtime, work_key="w1", required=("a", "b"))

    await offer_work(runtime, work)

    assert status_of(runtime, work) is WorkStatus.BLOCKED_CAPABILITY
    event = runtime.event_store.by_type("capability_missing")[0]
    assert event.payload["match_status"] == "COMPOSITION_REQUIRED"
    # Nothing is *missing*; the gap is that no one process covers both.
    assert event.payload["missing_capabilities"] == []
    assert runtime.work_requirement_store.get(work.id).missing_capabilities == []
    runtime.close()


async def test_full_coverage_spawns_the_capable_process(tmp_path):
    """AT7 + AT3 at the work level."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "combined", ("a", "b"))
    work = make_work(runtime, work_key="w1", required=("a", "b"))

    await offer_work(runtime, work)

    stored = runtime.work_requirement_store.get(work.id)
    assert stored.status is WorkStatus.SATISFIED
    assert stored.selected_definition_name == "combined"
    assert stored.selected_definition_version == "1"
    assert len(instances_named(runtime, "combined")) == 1
    assert runtime.event_store.by_type("capability_missing") == []
    runtime.close()


async def test_blocked_work_is_listed_for_an_operator(tmp_path):
    """Spec §94: what can this system not currently do?"""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "p1", ("a",))
    doable = make_work(runtime, work_key="ok", required=("a",))
    stuck = make_work(runtime, work_key="stuck", required=("z",))

    await offer_work(runtime, doable)
    await offer_work(runtime, stuck)

    blocked = runtime.get_blocked_capability_work()
    assert [w.work_key for w in blocked] == ["stuck"]
    assert blocked[0].missing_capabilities == ["z"]
    runtime.close()


async def test_an_already_running_process_is_not_respawned(tmp_path):
    """AT11: capability matching does not override work identity."""
    from nexus_seed.capabilities.models import CapabilityRef
    from nexus_seed.context.requirements import ContextRequirements, ContinuationReq
    from nexus_seed.core.process import ProcessDefinition, ProcessStatus

    async def waits(ctx):
        if ctx.resume_point == "resumed":
            ctx.satisfy_work()
            return ctx.complete(output={"done": True})
        return ctx.suspend(
            resume_point="resumed",
            waiting_for={"event_type": "wake_up"},
            saved_process_state={},
        )

    runtime = capability_runtime(tmp_path)
    runtime.register_process(
        ProcessDefinition(
            name="waiter",
            version="1",
            handler="waiter",
            provides_capabilities=(CapabilityRef("a"),),
            context_requirements=ContextRequirements(
                include_trigger_event=True, continuation=ContinuationReq(include=True)
            ),
        ),
        waits,
    )

    work = make_work(runtime, work_key="w1", required=("a",))
    await offer_work(runtime, work)
    assert instances_named(runtime, "waiter")[0].status is ProcessStatus.SUSPENDED

    # Offer the same requirement again: a capable process exists *and* is busy.
    await offer_work(runtime, work)

    assert len(instances_named(runtime, "waiter")) == 1
    assert status_of(runtime, work) is WorkStatus.MATCHED
    runtime.close()


async def test_a_completed_process_is_not_respawned(tmp_path):
    """AT12: existing work-matching semantics are preserved."""
    runtime = capability_runtime(tmp_path)
    register_capable(runtime, "p1", ("a",))
    work = make_work(runtime, work_key="w1", required=("a",))

    await offer_work(runtime, work)
    assert len(instances_named(runtime, "p1")) == 1

    await offer_work(runtime, work)

    assert len(instances_named(runtime, "p1")) == 1
    runtime.close()


async def test_the_match_status_is_recorded_even_when_blocked(tmp_path):
    runtime = capability_runtime(tmp_path)
    work = make_work(runtime, work_key="w1", required=("z",))

    await offer_work(runtime, work)

    attempts = runtime.get_capability_matches(work.id)
    assert len(attempts) == 1
    assert attempts[0].status is CapabilityMatchStatus.MISSING_CAPABILITY
    assert attempts[0].missing_capabilities == ["z"]
    assert attempts[0].selected_definition_name is None
    runtime.close()
