"""AT20, AT22 (spec §112, §114): a model may elaborate a route, not choose one.

The division is Phase 4C's, applied to the thing that would eventually change
the system itself (Invariant 87): the *analysis* is deterministic, and the model
is asked only to describe one of the routes that analysis found.  What it is
never asked for is as deliberate as what it is — no shell commands (spec §32),
no patches, no source code (spec §33) — because nothing in this phase may
execute any of that, and material that looks executable invites somebody to.

And it is optional in the strong sense (spec §81–§82): with the backend down,
the system still notices what it is missing and still asks about it.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    ExtensionProposalStatus,
    ExtensionRisk,
    block,
    elaborates,
    exhaust_retries,
    extension_runtime,
    failure_response,
    gap_work,
    install_extension_llm,
    invalid_response,
    manual_runtime,
    needs,
    only_gap,
    only_proposal,
)

from nexus_seed.extension.builder import EXTENSION_SCHEMA, LLMExtensionProposer


def ppt_work(runtime, **kwargs):
    return gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)], **kwargs)


# --- AT20: an elaborated proposal -------------------------------------------


async def test_a_model_elaborates_an_offered_route(tmp_path):
    runtime = extension_runtime(tmp_path)
    backend = install_extension_llm(
        runtime, elaborates("ADD_EXTRACTOR", capability="parse_powerpoint")
    )

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.source == "llm"
    assert proposal.declared_strategy == "ADD_EXTRACTOR"
    assert proposal.title == "Add a PowerPoint extractor"
    assert proposal.status is ExtensionProposalStatus.REVIEW
    assert len(backend.calls) == 1
    runtime.close()


async def test_the_call_is_journalled_and_linked(tmp_path):
    """spec §54: what the system believed it could do, at that moment."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime, elaborates("ADD_EXTRACTOR", capability="parse_powerpoint")
    )
    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    invocation = runtime.get_llm_invocation(proposal.llm_invocation_id)

    assert invocation is not None
    assert invocation.success
    assert invocation.request_metadata["capability_gap_id"] == str(
        proposal.capability_gap_id
    )
    assert proposal.context_snapshot_id is not None
    assert runtime.context_snapshot_store.get(proposal.context_snapshot_id) is not None
    runtime.close()


async def test_the_model_is_shown_the_gap_and_the_routes_only(tmp_path):
    """spec §53: not the whole registry — the gap and its candidates."""
    runtime = extension_runtime(tmp_path)
    backend = install_extension_llm(
        runtime, elaborates("ADD_EXTRACTOR", capability="parse_powerpoint")
    )
    await block(runtime, ppt_work(runtime))

    request = backend.calls[0]
    assert request.output_schema == EXTENSION_SCHEMA
    assert request.metadata["candidate_strategies"] == ["ADD_EXTRACTOR"]
    assert request.metadata["target_capabilities"] == ["parse_powerpoint"]
    assert request.metadata["capability_gap"]["missing_capabilities"] == [
        "parse_powerpoint"
    ]
    # No commands, no code, no patch anywhere in what is asked for (§32–§33).
    assert "shell" not in str(EXTENSION_SCHEMA).lower()
    assert "patch" not in str(EXTENSION_SCHEMA).lower()
    assert "code" not in str(EXTENSION_SCHEMA).lower()
    runtime.close()


async def test_the_risk_is_recomputed_not_taken_from_the_model(tmp_path):
    """A model rating its own extension LOW does not make it low (spec §40)."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime,
        elaborates(
            "ADD_EXTRACTOR",
            capability="parse_powerpoint",
            risk="LOW",
        ),
    )

    await block(runtime, ppt_work(runtime))

    assert only_proposal(runtime).estimated_risk is ExtensionRisk.MEDIUM
    runtime.close()


async def test_targets_come_from_the_gap_not_the_model(tmp_path):
    """spec §74: a model describes how to close this gap, not what it is."""
    runtime = extension_runtime(tmp_path)
    install_extension_llm(
        runtime, elaborates("ADD_EXTRACTOR", capability="parse_powerpoint")
    )

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.target_names == only_gap(runtime).missing_names
    runtime.close()


# --- AT22: the model is optional --------------------------------------------


async def test_a_backend_failure_falls_back_to_the_derived_proposal(tmp_path):
    """spec §114: the LLM is not a single point of failure."""
    runtime, clock = manual_runtime(tmp_path)
    backend = install_extension_llm(runtime, failure_response("connection refused"))

    await block(runtime, ppt_work(runtime))
    await exhaust_retries(runtime, clock)

    proposal = only_proposal(runtime)
    assert proposal.source == "deterministic"
    assert proposal.declared_strategy == "ADD_EXTRACTOR"
    assert any("llm proposer unavailable" in r for r in proposal.reasons)
    assert proposal.status is ExtensionProposalStatus.REVIEW
    # The gap is not lost, and it is not resolved either.
    assert only_gap(runtime).status.value == "PROPOSAL_AVAILABLE"
    # Retried first (max_retries=2), then derived.
    assert len(backend.calls) == 3
    runtime.close()


async def test_unreadable_output_falls_back_the_same_way(tmp_path):
    runtime, clock = manual_runtime(tmp_path)
    install_extension_llm(runtime, invalid_response())

    await block(runtime, ppt_work(runtime))
    await exhaust_retries(runtime, clock)

    proposal = only_proposal(runtime)
    assert proposal.source == "deterministic"
    assert any("unparseable" in r for r in proposal.reasons)
    runtime.close()


async def test_a_failed_call_is_still_journalled(tmp_path):
    """Invariant 26: the calls worth explaining are the ones that failed."""
    runtime, clock = manual_runtime(tmp_path)
    install_extension_llm(runtime, failure_response("timeout"))

    await block(runtime, ppt_work(runtime))
    await exhaust_retries(runtime, clock)

    instances = [
        i
        for i in runtime.process_store.all_instances()
        if i.definition_name == "analyze_capability_gap"
    ]
    invocations = runtime.get_llm_invocations(instances[0].id)
    assert invocations
    assert all(not i.success for i in invocations)
    assert any("timeout" in (i.error or "") for i in invocations)
    runtime.close()


async def test_with_no_model_at_all_the_loop_still_runs(tmp_path):
    """spec §82: the deterministic builder is the floor, not the fallback."""
    runtime = extension_runtime(tmp_path)
    assert runtime.llm_extension_proposer is None

    await block(runtime, ppt_work(runtime))

    proposal = only_proposal(runtime)
    assert proposal.source == "deterministic"
    assert proposal.llm_invocation_id is None
    assert proposal.status is ExtensionProposalStatus.REVIEW
    runtime.close()


# --- the shortlist ----------------------------------------------------------


def test_the_shortlist_drops_the_worst_routes_not_arbitrary_ones():
    from nexus_seed.extension.models import (
        AcquisitionCandidate,
        AcquisitionFeasibility,
        ExtensionStrategy,
    )

    candidates = [
        AcquisitionCandidate(strategy=s, feasibility=AcquisitionFeasibility.FEASIBLE)
        for s in (
            ExtensionStrategy.REGISTER_EXISTING_PROCESS,
            ExtensionStrategy.ADD_EXTRACTOR,
            ExtensionStrategy.CODE_EXTENSION,
        )
    ]
    shortlist = LLMExtensionProposer(None, max_candidates=2).shortlist(candidates)

    assert [c.strategy.value for c in shortlist] == [
        "REGISTER_EXISTING_PROCESS",
        "ADD_EXTRACTOR",
    ]


def test_the_shortlist_never_offers_an_unserviceable_route():
    from nexus_seed.extension.models import (
        AcquisitionCandidate,
        AcquisitionFeasibility,
        ExtensionStrategy,
    )

    candidates = [
        AcquisitionCandidate(
            strategy=ExtensionStrategy.UNSUPPORTED,
            feasibility=AcquisitionFeasibility.UNSUPPORTED,
        )
    ]
    assert LLMExtensionProposer(None).shortlist(candidates) == []
