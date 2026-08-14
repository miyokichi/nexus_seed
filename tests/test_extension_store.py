"""AT9 (spec §101): what the system proposed about itself is durable.

A proposal that did not survive a restart would be worse than no proposal: the
record of what this system wanted to become is exactly the part a reviewer comes
back to later.  Decisions are append-only for the same reason (Invariant 92).
"""

from __future__ import annotations

import uuid

from extension_helpers import (
    POWERPOINT,
    ExtensionProposalStatus,
    ExtensionRisk,
    ExtensionStrategy,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    reopen,
)

from nexus_seed.extension.models import (
    CapabilityGapStatus,
    ExtensionDecision,
    ExtensionDecisionRecord,
    ExtensionProposal,
    ProposedComponent,
)


async def test_a_proposal_reads_back_identical(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    before = only_proposal(runtime)
    runtime.close()

    rebuilt = reopen(tmp_path)
    after = rebuilt.get_extension_proposal(before.id)

    assert after.capability_gap_id == before.capability_gap_id
    assert after.work_requirement_id == requirement.id
    assert after.strategy is ExtensionStrategy.ADD_EXTRACTOR
    assert after.declared_strategy == before.declared_strategy
    assert after.title == before.title
    assert after.description == before.description
    assert after.target_names == before.target_names
    assert after.required_permissions == before.required_permissions
    assert after.estimated_risk is before.estimated_risk
    assert after.feasibility is before.feasibility
    assert after.status is before.status
    assert after.fingerprint == before.fingerprint
    assert after.candidate_strategies == before.candidate_strategies
    assert after.analysis == before.analysis
    rebuilt.close()


async def test_components_read_back_in_full(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    before = only_proposal(runtime).proposed_components[0]
    runtime.close()

    rebuilt = reopen(tmp_path)
    after = rebuilt.get_extension_proposals()[0].proposed_components[0]

    assert after.component_type == before.component_type == "RESOURCE_EXTRACTOR"
    assert after.name == before.name
    assert after.purpose == before.purpose
    assert after.provides_capabilities == before.provides_capabilities
    assert after.metadata == before.metadata
    rebuilt.close()


async def test_the_gap_reads_back_with_its_requirements(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(
        runtime,
        required=[needs("parse_powerpoint", POWERPOINT), needs("summarize", None)],
    )
    await block(runtime, requirement)
    before = only_gap(runtime)
    runtime.close()

    rebuilt = reopen(tmp_path)
    after = rebuilt.get_capability_gap(before.id)

    assert after.missing_names == before.missing_names
    assert after.missing_key == before.missing_key
    # The acquisition hints survive — they are what makes re-analysis possible.
    hints = [r.metadata.get("extension") for r in after.missing_capabilities]
    assert POWERPOINT in hints
    rebuilt.close()


async def test_a_duplicate_fingerprint_keeps_the_first_proposal(tmp_path):
    """Two derivations of the same content are one proposal (spec §68)."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    first = only_proposal(runtime)

    twin = ExtensionProposal(
        capability_gap_id=first.capability_gap_id,
        target_capabilities=list(first.target_capabilities),
        strategy=first.strategy,
        proposed_components=list(first.proposed_components),
        reusable_components=list(first.reusable_components),
    )
    assert twin.fingerprint == first.fingerprint
    stored = runtime.extension_store.save_proposal(twin)

    assert stored.id == first.id
    assert len(runtime.get_extension_proposals()) == 1
    runtime.close()


async def test_decisions_accumulate_rather_than_overwrite(tmp_path):
    """Invariant 92: a second decision is a second row."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)

    runtime.extension_store.save_decision(
        ExtensionDecisionRecord(
            extension_proposal_id=proposal.id,
            decision=ExtensionDecision.APPROVE,
            estimated_risk=ExtensionRisk.LOW,
            reasons=["a later change of mind"],
        )
    )

    decisions = runtime.get_extension_decisions(proposal.id)
    assert len(decisions) == 2
    assert [d.decision.value for d in decisions] == ["REVIEW", "APPROVE"]
    runtime.close()


async def test_status_queries_filter(tmp_path):
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)

    assert len(runtime.get_extension_proposals(ExtensionProposalStatus.REVIEW)) == 1
    assert runtime.get_extension_proposals(ExtensionProposalStatus.APPROVED) == []
    assert len(runtime.get_capability_gaps(CapabilityGapStatus.PROPOSAL_AVAILABLE)) == 1
    assert runtime.get_capability_gaps(CapabilityGapStatus.RESOLVED) == []
    runtime.close()


async def test_gaps_are_listed_per_need(tmp_path):
    runtime = extension_runtime(tmp_path)
    first = gap_work(runtime, work_key="a", required=[needs("parse_powerpoint")])
    second = gap_work(runtime, work_key="b", required=[needs("parse_keynote")])
    await block(runtime, first)
    await block(runtime, second)

    assert len(runtime.get_capability_gaps_for_work(first.id)) == 1
    assert len(runtime.get_capability_gaps_for_work(second.id)) == 1
    assert runtime.get_capability_gaps_for_work(uuid.uuid4()) == []
    runtime.close()


async def test_an_unknown_proposal_is_none(tmp_path):
    runtime = extension_runtime(tmp_path)
    assert runtime.get_extension_proposal(uuid.uuid4()) is None
    assert runtime.get_extension_decisions(uuid.uuid4()) == []
    runtime.close()


def test_a_stored_unknown_component_type_stays_unknown(tmp_path):
    """Round-tripping never launders an unreadable component into a known one."""
    runtime = extension_runtime(tmp_path)
    gap = runtime.extension_store.save_gap(
        __import__("nexus_seed.extension.models", fromlist=["CapabilityGap"]).CapabilityGap(
            work_requirement_id=uuid.uuid4(),
            missing_capabilities=[needs("x")],
        )
    )
    proposal = ExtensionProposal(
        capability_gap_id=gap.id,
        strategy=ExtensionStrategy.CODE_EXTENSION,
        target_capabilities=[needs("x")],
        proposed_components=[ProposedComponent(component_type="WORMHOLE", name="w")],
    )
    runtime.extension_store.save_proposal(proposal)

    reloaded = runtime.get_extension_proposal(proposal.id)
    assert reloaded.proposed_components[0].component_type == "WORMHOLE"
    runtime.close()
