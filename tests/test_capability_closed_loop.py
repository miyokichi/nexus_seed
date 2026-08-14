"""AT19 + AT20 + AT21 (spec §75–§77): the whole loop, gated on competence.

The complete chain — external file to external file — now passes through the
question *can we do this?*.  Two runs of the identical scenario, one with the
capability and one without, and the difference is visible all the way out to
whether a file appears on disk.
"""

from __future__ import annotations

from capability_helpers import instances_named
from resource_helpers import FACT_DOCUMENT, full_stack, watched_tree, write_file

from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


async def run_loop(tmp_path, *, db="loop.db"):
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / db)
    adapter, backend = full_stack(runtime, root, out)
    write_file(root, "report.txt", FACT_DOCUMENT)
    return runtime, adapter, backend, root, out


async def test_the_loop_runs_through_capability_matching(tmp_path):
    """AT19."""
    runtime, adapter, backend, root, out = await run_loop(tmp_path)

    await adapter.poll_and_ingest()

    requirement = runtime.get_work_requirements()[0]
    assert requirement.work_type == "write_analysis_result"
    assert [r.name for r in requirement.required_capabilities] == [
        "generate_analysis_report"
    ]
    assert requirement.selected_definition_name == "write_analysis_result"
    assert requirement.status is WorkStatus.SATISFIED

    assert (out / "D1_CD_analysis.txt").exists()
    assert len(backend.calls) == 1

    trace = runtime.get_capability_trace(requirement.id)
    assert trace.selected == ("write_analysis_result", "1")
    assert not trace.was_blocked
    runtime.close()


async def test_without_the_capability_the_world_is_left_alone(tmp_path):
    """AT20: state updates, work is recorded, nothing reaches the outside."""
    runtime, adapter, backend, root, out = await run_loop(tmp_path)
    runtime.set_capability_enabled("generate_analysis_report", "1", False)

    await adapter.poll_and_ingest()

    # Perception still happened.
    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"
    # The need is recorded and held.
    requirement = runtime.get_work_requirements()[0]
    assert requirement.status is WorkStatus.BLOCKED_CAPABILITY
    assert requirement.missing_capabilities == ["generate_analysis_report"]
    # And the world was not touched.
    assert runtime.get_action_proposals() == []
    assert backend.calls == []
    assert list(out.glob("*.txt")) == []
    runtime.close()


async def test_restoring_the_capability_completes_the_loop(tmp_path):
    """AT21: the held need turns into a real file once we can do it."""
    runtime, adapter, backend, root, out = await run_loop(tmp_path)
    runtime.set_capability_enabled("generate_analysis_report", "1", False)
    await adapter.poll_and_ingest()

    requirement = runtime.get_work_requirements()[0]
    assert requirement.status is WorkStatus.BLOCKED_CAPABILITY

    runtime.set_capability_enabled("generate_analysis_report", "1", True)
    await runtime.run_pending()

    assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    assert (out / "D1_CD_analysis.txt").exists()
    assert len(backend.calls) == 1
    # One requirement, one action, one file — no duplication from the detour.
    assert len(runtime.get_work_requirements()) == 1
    assert len(runtime.get_action_proposals()) == 1
    assert len(list(out.glob("*.txt"))) == 1
    runtime.close()


async def test_the_detour_is_visible_in_the_audit(tmp_path):
    runtime, adapter, backend, root, out = await run_loop(tmp_path)
    runtime.set_capability_enabled("generate_analysis_report", "1", False)
    await adapter.poll_and_ingest()
    runtime.set_capability_enabled("generate_analysis_report", "1", True)
    await runtime.run_pending()

    requirement = runtime.get_work_requirements()[0]
    trace = runtime.get_capability_trace(requirement.id)

    assert len(trace.attempts) == 2
    assert trace.was_blocked
    assert trace.attempts[0].missing_capabilities == ["generate_analysis_report"]
    assert trace.selected == ("write_analysis_result", "1")
    runtime.close()


async def test_restoring_after_a_restart_still_completes(tmp_path):
    """The held need and the missing competence are both durable."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    db_path = tmp_path / "loop.db"

    runtime = Runtime(db_path)
    adapter, backend = full_stack(runtime, root, out)
    runtime.set_capability_enabled("generate_analysis_report", "1", False)
    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()
    requirement_id = runtime.get_work_requirements()[0].id
    runtime.close()

    runtime2 = Runtime(db_path)
    adapter2, backend2 = full_stack(runtime2, root, out)
    assert runtime2.get_blocked_capability_work()[0].id == requirement_id

    runtime2.set_capability_enabled("generate_analysis_report", "1", True)
    await runtime2.run_pending()

    assert runtime2.get_work_requirement(requirement_id).status is WorkStatus.SATISFIED
    assert (out / "D1_CD_analysis.txt").exists()
    assert len(backend2.calls) == 1
    runtime2.close()


async def test_a_capability_gap_does_not_break_the_rest_of_the_pipeline(tmp_path):
    """Everything upstream of the gap still works normally."""
    runtime, adapter, backend, root, out = await run_loop(tmp_path)
    runtime.set_capability_enabled("generate_analysis_report", "1", False)

    await adapter.poll_and_ingest()

    resource = runtime.get_resource_by_uri("file:///report.txt")
    version = runtime.get_current_resource_version(resource.id)
    assert runtime.find_representation(version.id, "text") is not None
    assert runtime.observation_store.all() != []
    assert runtime.get_pending_event_delivery_count() == 0
    assert runtime.get_failed_event_deliveries() == []
    runtime.close()
