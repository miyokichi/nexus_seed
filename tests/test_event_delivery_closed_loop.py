"""AT17 + AT18 (spec §68, §69): crash anywhere in the loop; finish once.

The whole system, interrupted at its most dangerous moment — after the world's
event is durable but before anything has reacted to it — and driven to
completion by the delivery ledger alone.  Then the convergence check: however
many restarts and retries happened, the world changed exactly once.
"""

from __future__ import annotations

from datetime import datetime, timezone

from resource_helpers import FACT_DOCUMENT, full_stack, watched_tree, write_file

from nexus_seed.adapters.file_watch import LocalFileAdapter
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus

ALL_ONE = {
    "receipts": 1,
    "events_of_each_kind": 1,
    "resources": 1,
    "versions": 1,
    "representations": 1,
    "state_versions": 1,
    "work_requirements": 1,
    "action_proposals": 1,
    "output_files": 1,
    "outstanding_deliveries": 0,
}


def counts(runtime, out) -> dict:
    resource = runtime.get_resource_by_uri("file:///report.txt")
    versions = runtime.get_resource_versions(resource.id)
    kinds = {
        len(runtime.event_store.by_type(t))
        for t in (
            "file_created",
            "resource_version_created",
            "representation_created",
            "action_succeeded",
        )
    }
    return {
        "receipts": len(runtime.get_ingress_receipts()),
        "events_of_each_kind": max(kinds),
        "resources": len(runtime.get_resources()),
        "versions": len(versions),
        "representations": len(runtime.get_representations(versions[-1].id)),
        "state_versions": len(runtime.get_state_history("D1_CD", "analysis_result")),
        "work_requirements": len(runtime.get_work_requirements()),
        "action_proposals": len(runtime.get_action_proposals()),
        "output_files": len(list(out.glob("*.txt"))),
        "outstanding_deliveries": runtime.get_pending_event_delivery_count(),
    }


async def test_a_crash_before_any_routing_still_completes_the_loop(tmp_path):
    """AT17."""
    db_path = tmp_path / "loop.db"
    root = watched_tree(tmp_path)
    out = tmp_path / "out"

    runtime = Runtime(db_path)
    adapter = LocalFileAdapter(root).bind(runtime.ingress)
    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest(deliver=False)

    assert runtime.process_store.all_instances() == []
    assert runtime.get_pending_event_delivery_count() == 1
    runtime.close()

    runtime2 = Runtime(db_path)
    adapter2, backend2 = full_stack(runtime2, root, out)
    assert await adapter2.poll() == []  # the source will not re-offer it

    await runtime2.run_pending()

    assert runtime2.state_store.get("D1_CD", "analysis_result") == "within spec"
    assert runtime2.get_work_requirements()[0].status is WorkStatus.SATISFIED
    assert (out / "D1_CD_analysis.txt").exists()
    assert counts(runtime2, out) == ALL_ONE
    runtime2.close()


async def test_crashing_repeatedly_mid_loop_still_converges(tmp_path):
    """AT18: restart after every single step; one of everything at the end."""
    db_path = tmp_path / "loop.db"
    root = watched_tree(tmp_path)
    out = tmp_path / "out"

    runtime = Runtime(db_path)
    adapter = LocalFileAdapter(root).bind(runtime.ingress)
    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest(deliver=False)
    runtime.close()

    # Rebuild the whole runtime between every unit of work.
    for _ in range(40):
        runtime = Runtime(db_path)
        full_stack(runtime, root, out)
        runtime.dispatch_pending_events(limit=1)
        instance = runtime.scheduler.next_runnable()
        if instance is not None:
            await runtime.executor.execute(instance)
        idle = (
            runtime.get_pending_event_delivery_count() == 0
            and runtime.scheduler.next_runnable() is None
        )
        runtime.close()
        if idle:
            break

    runtime = Runtime(db_path)
    full_stack(runtime, root, out)
    await runtime.run_pending()

    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"
    assert counts(runtime, out) == ALL_ONE
    runtime.close()


async def test_a_routing_failure_mid_loop_delays_but_does_not_break_it(tmp_path):
    """A transient router fault costs a retry, not the outcome."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "loop.db", clock=clock)
    adapter, backend = full_stack(runtime, root, out)

    from delivery_helpers import break_router, heal_router

    break_router(runtime, failures=1, only_type="representation_created")

    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()

    stalled = runtime.event_store.by_type("representation_created")[0]
    assert runtime.get_event_delivery(stalled.id).status.value == "RETRY_WAIT"
    assert runtime.state_store.get("D1_CD", "analysis_result") is None

    heal_router(runtime)
    clock.advance(60)
    await runtime.tick()

    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"
    assert counts(runtime, out) == ALL_ONE
    assert len(backend.calls) == 1
    runtime.close()


async def test_the_delivery_ledger_matches_the_event_store_at_rest(tmp_path):
    """When the system is idle, every event is accounted for."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "loop.db")
    adapter, backend = full_stack(runtime, root, out)

    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()

    events = runtime.event_store.all()
    deliveries = runtime.get_event_deliveries()
    assert {d.event_id for d in deliveries} == {e.id for e in events}
    assert all(d.status.value == "DELIVERED" for d in deliveries)
    assert runtime.get_failed_event_deliveries() == []
    runtime.close()
