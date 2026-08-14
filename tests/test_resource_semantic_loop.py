"""AT14, AT19–AT22: the full loop, with a real document in the middle.

    External file
      -> Ingress -> file event
        -> Resource + Version
          -> Representation
            -> interpret_resource -> Observation -> StateDelta -> World State
              -> WorkRequirement -> Process -> ActionProposal
                -> LocalFileActionBackend -> a file on disk

Every stage but the first two existed before Phase 3E.  What this proves is
that a *document* can travel the whole path without any of them changing — and
that the four idempotency mechanisms plus the new content-hash dedup all agree.
"""

from __future__ import annotations

from datetime import datetime, timezone

from resource_helpers import (
    FACT_DOCUMENT,
    full_stack,
    instances_named,
    watched_tree,
    write_file,
)

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.processes.resources import parse_facts
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


# --- the deterministic reader ---------------------------------------------


def test_fact_parsing_is_explicit_and_typed():
    facts = parse_facts(
        "# a comment\nD1_CD.target=45\nD1_CD.name=alpha\nD1_CD.ok=true\n\nbroken line\n"
    )
    assert facts == [
        ("D1_CD", "target", 45),
        ("D1_CD", "name", "alpha"),
        ("D1_CD", "ok", True),
    ]


def test_fact_parsing_ignores_unusable_content():
    assert parse_facts("") == []
    assert parse_facts({"not": "text"}) == []
    assert parse_facts("no separator here") == []


# --- AT14: document -> world state -----------------------------------------


async def test_a_document_reaches_world_state_through_the_normal_pipeline(tmp_path):
    """AT14 (spec §66): no private write path for documents."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "sem.db")
    adapter, _ = full_stack(runtime, root)

    write_file(root, "report.txt", "D1_CD.target=45\n")
    await adapter.poll_and_ingest()

    assert runtime.state_store.get("D1_CD", "target") == 45

    # It went through Observation + StateDelta like every other reading.
    observation = runtime.observation_store.all()[0]
    assert observation.predicate == "facts_extracted"
    delta = runtime.state_delta_store.all()[0]
    assert (delta.entity, delta.attribute, delta.new_value) == ("D1_CD", "target", 45)
    assert delta.observation_id == observation.id

    provenance = runtime.get_state_provenance("D1_CD", "target")
    assert provenance.source_event.type == "representation_created"
    runtime.close()


async def test_a_revised_document_updates_world_state_once(tmp_path):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "sem.db")
    adapter, _ = full_stack(runtime, root)

    write_file(root, "report.txt", "D1_CD.target=48\n")
    await adapter.poll_and_ingest()
    write_file(root, "report.txt", "D1_CD.target=45\n")
    await adapter.poll_and_ingest()

    assert runtime.state_store.get("D1_CD", "target") == 45
    assert len(runtime.get_state_history("D1_CD", "target")) == 2
    runtime.close()


async def test_a_document_restating_a_known_fact_changes_nothing(tmp_path):
    """A second document agreeing with world state is not a state change."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "sem.db")
    adapter, _ = full_stack(runtime, root)

    write_file(root, "a.txt", "D1_CD.target=45\n")
    await adapter.poll_and_ingest()
    write_file(root, "b.txt", "D1_CD.target=45\n")
    await adapter.poll_and_ingest()

    assert len(runtime.get_state_history("D1_CD", "target")) == 1
    assert len(runtime.state_delta_store.all()) == 1
    runtime.close()


# --- AT19 / AT20: all the way out again ------------------------------------


async def test_file_in_file_out(tmp_path):
    """AT19 + AT20: the closed loop, with a document at the start."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "loop.db")
    adapter, backend = full_stack(runtime, root, out)

    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()

    # --- resource layer ---
    resource = runtime.get_resource_by_uri("file:///report.txt")
    version = runtime.get_current_resource_version(resource.id)
    representation = runtime.find_representation(version.id, "text")
    assert representation.content == FACT_DOCUMENT

    # --- world state ---
    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"

    # --- work ---
    requirement = runtime.get_work_requirements()[0]
    assert requirement.work_type == "write_analysis_result"
    assert requirement.status is WorkStatus.SATISFIED
    worker = instances_named(runtime, "write_analysis_result")[0]
    assert worker.status is ProcessStatus.COMPLETED

    # --- action, and a real file on the other side ---
    proposal = runtime.get_action_proposals()[0]
    assert proposal.status.value == "SUCCEEDED"
    assert (out / "D1_CD_analysis.txt").exists()
    assert len(backend.calls) == 1

    # --- and the whole chain is traceable back to the document ---
    action_trace = runtime.get_action_trace(proposal.id)
    assert action_trace.source_event.type == "representation_created"
    representation_trace = runtime.get_representation_trace(representation.id)
    assert representation_trace.resource.uri == "file:///report.txt"
    assert representation_trace.ingress_receipt.adapter_id == "local_file"
    runtime.close()


ALL_ONE = {
    "receipts": 1,
    "resources": 1,
    "versions": 1,
    "representations": 1,
    "state_versions": 1,
    "work_requirements": 1,
    "action_proposals": 1,
    "output_files": 1,
}


def counts(runtime, out) -> dict:
    resource = runtime.get_resource_by_uri("file:///report.txt")
    versions = runtime.get_resource_versions(resource.id)
    return {
        "receipts": len(runtime.get_ingress_receipts()),
        "resources": len(runtime.get_resources()),
        "versions": len(versions),
        "representations": len(runtime.get_representations(versions[-1].id)),
        "state_versions": len(runtime.get_state_history("D1_CD", "analysis_result")),
        "work_requirements": len(runtime.get_work_requirements()),
        "action_proposals": len(runtime.get_action_proposals()),
        "output_files": len(list(out.glob("*.txt"))),
    }


async def test_rescanning_converges_on_one_of_everything(tmp_path):
    """AT21 (spec §73): five idempotency mechanisms, all agreeing."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "conv.db")
    adapter, backend = full_stack(runtime, root, out)

    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()
    assert counts(runtime, out) == ALL_ONE

    for _ in range(3):
        await adapter.poll_and_ingest()

    assert counts(runtime, out) == ALL_ONE
    assert len(backend.calls) == 1
    runtime.close()


async def test_the_loop_survives_a_restart_mid_flight(tmp_path):
    """AT22: crash after the file event is durable but before anything ran.

    The strongest version of the restart claim: nothing had been routed at all,
    the file scan will not re-offer the event (ingress dedup consumed the source
    key), and the whole loop still completes — because the *delivery obligation*
    outlived the crash (Invariant 47).
    """
    db_path = tmp_path / "restart.db"
    root = watched_tree(tmp_path)
    out = tmp_path / "out"

    from nexus_seed.adapters.file_watch import LocalFileAdapter

    runtime = Runtime(db_path)
    adapter = LocalFileAdapter(root).bind(runtime.ingress)
    write_file(root, "report.txt", FACT_DOCUMENT)
    # Persisted with its receipt and its delivery obligation; nothing routed.
    await adapter.poll_and_ingest(deliver=False)

    assert runtime.process_store.all_instances() == []
    assert runtime.get_resources() == []
    assert runtime.get_pending_event_delivery_count() == 1
    runtime.close()

    # --- rebuilt with the full stack ---
    runtime2 = Runtime(db_path)
    adapter2, backend2 = full_stack(runtime2, root, out)

    # A re-scan finds nothing new: the source key was already consumed.
    assert await adapter2.poll() == []

    # The outstanding delivery is what carries the work forward.
    await runtime2.run_pending()

    assert runtime2.state_store.get("D1_CD", "analysis_result") == "within spec"
    assert (out / "D1_CD_analysis.txt").exists()
    assert counts(runtime2, out) == ALL_ONE
    assert len(backend2.calls) == 1
    assert runtime2.get_pending_event_delivery_count() == 0

    # And a fresh scan afterwards still adds nothing.
    await adapter2.poll_and_ingest()
    assert counts(runtime2, out) == ALL_ONE
    runtime2.close()


async def test_the_observer_drives_the_whole_loop(tmp_path):
    """The resident process ties it together: ticks in, files out."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "obs_loop.db", clock=clock)
    adapter, backend = full_stack(runtime, root, out, observer=True)

    await runtime.submit_event(
        Event("start_watch_files", "operator", {"adapter_id": "local_file", "poll_interval": 30})
    )
    assert runtime.get_resources() == []

    write_file(root, "report.txt", FACT_DOCUMENT)
    clock.advance(30)
    await runtime.tick()

    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"
    assert (out / "D1_CD_analysis.txt").exists()
    assert len(backend.calls) == 1

    # Two more quiet ticks change nothing.
    for _ in range(2):
        clock.advance(30)
        await runtime.tick()
    assert counts(runtime, out) == ALL_ONE
    runtime.close()
