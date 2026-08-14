"""AT28 (spec §120): one continuous chain from a raw event to a self-extension.

    raw Event -> Observation -> StateDelta -> World State -> WorkRequirement
              -> CapabilityMatch -> CapabilityGap -> AcquisitionCandidates
              -> ExtensionProposal -> policy decision -> human review

Every link existed already; assembling them is what makes *"why does this system
want to change itself?"* a question with an answer.  The ContextSnapshot is the
other half: it records what the system believed it could do at the moment it
concluded it was insufficient (spec §54).
"""

from __future__ import annotations

import uuid

from extension_helpers import (
    elaborates,
    install_extension_llm,
    only_gap,
    only_proposal,
    reviewed,
)
from resource_helpers import FACT_DOCUMENT, full_stack, watched_tree, write_file

from nexus_seed.processes.extension import bootstrap_extension
from nexus_seed.runtime.runtime import Runtime

CAPABILITY = "generate_analysis_report"


async def traced(tmp_path, *, db="trace.db", llm=False):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / db)
    adapter, _backend = full_stack(runtime, root, tmp_path / "out")
    bootstrap_extension(runtime)
    if llm:
        install_extension_llm(
            runtime,
            elaborates(
                "REGISTER_EXISTING_PROCESS",
                capability=CAPABILITY,
                component_type="CONFIGURATION",
                component="enable_report_generator",
                permissions=("capability.enable",),
                risk="LOW",
                title="Re-enable the report generator",
            ),
        )
    runtime.set_capability_enabled(CAPABILITY, "1", False)
    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()
    return runtime


async def test_the_trace_reaches_the_raw_event(tmp_path):
    runtime = await traced(tmp_path)
    proposal = only_proposal(runtime)

    trace = runtime.get_extension_trace(proposal.id)

    assert trace is not None
    assert trace.gap.id == only_gap(runtime).id
    assert trace.work_requirement is not None
    assert trace.work_requirement.work_type == "write_analysis_result"
    assert trace.capability_match is not None
    assert trace.capability_match.missing_capabilities == [CAPABILITY]
    assert trace.state_delta is not None
    assert trace.state_delta.attribute == "analysis_result"
    assert trace.observation is not None
    assert trace.source_event is not None
    runtime.close()


async def test_the_source_event_came_from_outside(tmp_path):
    """The chain joins the ingress trace rather than duplicating it.

    The proposal's source event is an internal one (the extraction that made
    the fact readable), so "did this start outside?" is answered the way every
    other trace answers it: by the correlation the whole chain shares.
    """
    runtime = await traced(tmp_path)
    trace = runtime.get_extension_trace(only_proposal(runtime).id)

    correlation = trace.source_event.correlation_id
    assert correlation is not None

    # The correlation the whole chain carries *is* the event that entered from
    # outside — so the proposal, the gap and the file are one causal story.
    ingested = runtime.event_store.get(correlation)
    assert ingested.ingress_receipt_id is not None
    receipt = runtime.get_ingress_receipt(ingested.ingress_receipt_id)
    assert receipt.source_event_key
    assert receipt.adapter_id
    runtime.close()


async def test_the_trace_carries_the_routes_that_were_considered(tmp_path):
    runtime = await traced(tmp_path)
    trace = runtime.get_extension_trace(only_proposal(runtime).id)

    assert trace.candidate_strategies == ["REGISTER_EXISTING_PROCESS"]
    assert [c["strategy"] for c in trace.candidates] == ["REGISTER_EXISTING_PROCESS"]
    assert trace.candidates[0]["reusable_components"] == ["write_analysis_result:1"]
    runtime.close()


async def test_the_trace_shows_the_policy_decision_and_the_review(tmp_path):
    """spec §57: from the automatic decision to the person who answered it."""
    runtime = await traced(tmp_path)
    proposal = only_proposal(runtime)
    review = reviewed(proposal.id, "approve")
    await runtime.submit_event(review)

    trace = runtime.get_extension_trace(proposal.id)

    assert [d.decision.value for d in trace.decisions] == ["REVIEW", "APPROVE"]
    assert trace.final_decision.reviewed_by_event_id == review.id
    assert trace.human_reviewed
    assert trace.approved
    # And the distance this phase keeps: approved is not acquired.
    assert not trace.capability_acquired
    assert trace.decisions[0].policy["require_human_approval"] is True
    runtime.close()


async def test_the_trace_reaches_the_model_and_the_context_it_saw(tmp_path):
    """spec §54: what the system believed it could do at that moment."""
    runtime = await traced(tmp_path, llm=True)
    proposal = only_proposal(runtime)

    trace = runtime.get_extension_trace(proposal.id)

    assert trace.proposal.source == "llm"
    assert trace.llm_invocation is not None
    assert trace.llm_invocation.success
    assert trace.context_snapshot is not None
    assert trace.context_snapshot.id == proposal.context_snapshot_id
    runtime.close()


async def test_the_trace_survives_a_restart(tmp_path):
    runtime = await traced(tmp_path)
    proposal_id = only_proposal(runtime).id
    await runtime.submit_event(reviewed(proposal_id, "approve"))
    runtime.close()

    rebuilt = Runtime(tmp_path / "trace.db")
    trace = rebuilt.get_extension_trace(proposal_id)

    assert trace.work_requirement is not None
    assert trace.source_event is not None
    assert trace.human_reviewed
    assert len(trace.lineage) == 1
    rebuilt.close()


async def test_an_unknown_proposal_traces_to_nothing(tmp_path):
    runtime = await traced(tmp_path)
    assert runtime.get_extension_trace(uuid.uuid4()) is None
    runtime.close()
