"""AT30 (spec §122): the whole loop, from a file on disk to an approved proposal.

    external file
      -> ingress -> Resource -> Representation -> Observation -> StateDelta
      -> World State -> WorkRequirement -> MISSING_CAPABILITY
      -> BLOCKED_CAPABILITY -> CapabilityGap -> acquisition analysis
      -> ExtensionProposal -> validation -> policy -> human review -> APPROVED

and there it stops.  The capability is *not* acquired, the file is *not*
written, and the work is *not* satisfied — which is the correct end state for
this phase, and the reason the test asserts all three.
"""

from __future__ import annotations

from extension_helpers import (
    CapabilityGapStatus,
    ExtensionProposalStatus,
    only_gap,
    only_proposal,
    reviewed,
)
from resource_helpers import FACT_DOCUMENT, full_stack, watched_tree, write_file

from nexus_seed.processes.extension import bootstrap_extension
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus

CAPABILITY = "generate_analysis_report"


async def run_loop(tmp_path, *, db="loop.db"):
    """The Phase 3E/4A stack, plus self-extension, with the competence removed."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / db)
    adapter, backend = full_stack(runtime, root, out)
    bootstrap_extension(runtime)
    # The system can no longer do the work it is about to be asked for.
    runtime.set_capability_enabled(CAPABILITY, "1", False)
    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()
    return runtime, backend, out


async def test_the_loop_reaches_a_proposal_and_stops(tmp_path):
    runtime, backend, out = await run_loop(tmp_path)

    # Perception happened as normal: the gap is downstream of understanding.
    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"

    requirement = runtime.get_work_requirements()[0]
    assert requirement.status is WorkStatus.BLOCKED_CAPABILITY
    assert requirement.missing_capabilities == [CAPABILITY]

    gap = only_gap(runtime)
    assert gap.work_requirement_id == requirement.id
    assert gap.missing_names == [CAPABILITY]

    proposal = only_proposal(runtime)
    # It knows it already has the process — it is simply switched off.
    assert proposal.declared_strategy == "REGISTER_EXISTING_PROCESS"
    assert "write_analysis_result:1" in proposal.reusable_components
    assert proposal.status is ExtensionProposalStatus.REVIEW

    # And nothing has happened to the world.
    assert backend.calls == []
    assert list(out.glob("*.txt")) == []
    runtime.close()


async def test_approval_ends_the_loop_without_acquiring_anything(tmp_path):
    runtime, backend, out = await run_loop(tmp_path)
    proposal = only_proposal(runtime)

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    assert runtime.get_extension_proposal(proposal.id).status is (
        ExtensionProposalStatus.APPROVED
    )
    # Capability newly activated = 0 (spec §123).
    assert not runtime.get_capability(CAPABILITY, "1").enabled
    assert only_gap(runtime).status is CapabilityGapStatus.PROPOSAL_APPROVED
    # Work remains unresolved, and the world is untouched.
    assert runtime.get_work_requirements()[0].status is WorkStatus.BLOCKED_CAPABILITY
    assert backend.calls == []
    assert list(out.glob("*.txt")) == []
    runtime.close()


async def test_the_loop_completes_if_a_person_actually_acts_on_it(tmp_path):
    """The other half of the story: acquisition is a human act in Phase 5A."""
    runtime, backend, out = await run_loop(tmp_path)
    proposal = only_proposal(runtime)
    await runtime.submit_event(reviewed(proposal.id, "approve"))

    # A person reads the approved proposal and does what it says.
    runtime.set_capability_enabled(CAPABILITY, "1", True)
    await runtime.run_pending()

    assert only_gap(runtime).status is CapabilityGapStatus.RESOLVED
    assert runtime.get_work_requirements()[0].status is WorkStatus.SATISFIED
    assert (out / "D1_CD_analysis.txt").exists()
    assert len(backend.calls) == 1
    runtime.close()


async def test_the_gap_did_not_disturb_the_rest_of_the_pipeline(tmp_path):
    runtime, backend, out = await run_loop(tmp_path)

    resource = runtime.get_resource_by_uri("file:///report.txt")
    version = runtime.get_current_resource_version(resource.id)
    assert runtime.find_representation(version.id, "text") is not None
    assert runtime.observation_store.all() != []
    assert runtime.get_pending_event_delivery_count() == 0
    assert runtime.get_failed_event_deliveries() == []
    assert [
        i.status.value
        for i in runtime.process_store.all_instances()
        if i.definition_name == "analyze_capability_gap"
    ] == ["SUSPENDED"]
    runtime.close()


async def test_the_events_tell_the_story_in_order(tmp_path):
    runtime, backend, out = await run_loop(tmp_path)
    await runtime.submit_event(reviewed(only_proposal(runtime).id, "approve"))

    types = [e.type for e in runtime.event_store.all()]
    story = [
        t
        for t in types
        if t
        in {
            "capability_missing",
            "capability_gap_opened",
            "extension_proposed",
            "extension_review_required",
            "extension_reviewed",
            "extension_approved",
        }
    ]
    assert story == [
        "capability_missing",
        "capability_gap_opened",
        "extension_proposed",
        "extension_review_required",
        "extension_reviewed",
        "extension_approved",
    ]
    # Never emitted in this phase: there is nothing that could emit them.
    assert "capability_acquired" not in types
    assert "extension_constructed" not in types
    runtime.close()
