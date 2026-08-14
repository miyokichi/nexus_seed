"""AT16, AT17 (spec §108, §109): a person decides, and approval acquires nothing.

The two outcomes that matter are asymmetric on purpose.  Approval means "this
description may be handed to a construction phase" and *nothing else*: the
capability registry is untouched, and the gap stays open because we still cannot
do the thing (Invariant 89).  Rejection refuses one route and leaves the
deficiency exactly as real as it was.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    CapabilityGapStatus,
    ExtensionProposalStatus,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    register_disabled_provider,
    reviewed,
)


async def blocked_with_proposal(tmp_path, name="review.db"):
    runtime = extension_runtime(tmp_path, name)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    return runtime, requirement, only_proposal(runtime)


# --- AT16: approval ---------------------------------------------------------


async def test_approval_marks_the_proposal_and_nothing_else(tmp_path):
    runtime, requirement, proposal = await blocked_with_proposal(tmp_path)

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    decided = runtime.get_extension_proposal(proposal.id)
    assert decided.status is ExtensionProposalStatus.APPROVED
    # Capability Registry changes = 0 (spec §108).
    assert runtime.get_capability("parse_powerpoint") is None
    assert runtime.list_capabilities() == []
    runtime.close()


async def test_approval_does_not_resolve_the_gap(tmp_path):
    """AT26 (spec §118): approved is not acquired."""
    runtime, requirement, proposal = await blocked_with_proposal(tmp_path)

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    gap = only_gap(runtime)
    assert gap.status is CapabilityGapStatus.PROPOSAL_APPROVED
    assert gap.status is not CapabilityGapStatus.RESOLVED
    assert not gap.status.terminal
    assert runtime.get_open_capability_gaps() == [gap]
    # And the work is still blocked, because we still cannot do it.
    assert runtime.get_work_requirement(requirement.id).status.value == (
        "BLOCKED_CAPABILITY"
    )
    runtime.close()


async def test_approval_records_who_decided_and_why(tmp_path):
    runtime, _requirement, proposal = await blocked_with_proposal(tmp_path)
    review = reviewed(proposal.id, "approve")

    await runtime.submit_event(review)

    decisions = runtime.get_extension_decisions(proposal.id)
    assert [d.decision.value for d in decisions] == ["REVIEW", "APPROVE"]
    assert decisions[-1].reviewed_by_event_id == review.id
    assert decisions[0].reviewed_by_event_id is None
    types = [e.type for e in runtime.event_store.all()]
    assert "extension_approved" in types
    runtime.close()


async def test_approval_revalidates_against_the_current_world(tmp_path):
    """A capability registered during the review makes the extension pointless."""
    runtime, _requirement, proposal = await blocked_with_proposal(tmp_path)

    # Somebody registers a real provider while the proposal sits in review.
    from extension_helpers import register_capable

    register_capable(runtime, "ppt_reader", ("parse_powerpoint",))
    await runtime.submit_event(reviewed(proposal.id, "approve"))

    decided = runtime.get_extension_proposal(proposal.id)
    assert decided.status is ExtensionProposalStatus.INVALID
    assert any("already provided" in r for r in decided.reasons)
    runtime.close()


async def test_a_reuse_proposal_can_be_approved_without_being_applied(tmp_path):
    """Approving "switch it back on" does not switch it back on (spec §89)."""
    runtime = extension_runtime(tmp_path)
    register_disabled_provider(runtime, "report_generator", "generate_report")
    requirement = gap_work(runtime, required=[needs("generate_report")])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)

    await runtime.submit_event(reviewed(proposal.id, "approve"))

    assert runtime.get_extension_proposal(proposal.id).status.value == "APPROVED"
    # The capability is still disabled: applying the proposal is Phase 5B.
    assert not runtime.get_capability("generate_report", "1").enabled
    assert runtime.get_work_requirement(requirement.id).status.value == (
        "BLOCKED_CAPABILITY"
    )
    runtime.close()


# --- AT17: rejection --------------------------------------------------------


async def test_rejection_leaves_the_gap_open(tmp_path):
    runtime, _requirement, proposal = await blocked_with_proposal(tmp_path)

    await runtime.submit_event(reviewed(proposal.id, "reject"))

    decided = runtime.get_extension_proposal(proposal.id)
    assert decided.status is ExtensionProposalStatus.REJECTED
    gap = only_gap(runtime)
    assert gap.status is CapabilityGapStatus.OPEN
    assert not gap.status.terminal
    assert gap.missing_names == ["parse_powerpoint"]
    runtime.close()


async def test_rejection_is_recorded_with_its_reason(tmp_path):
    runtime, _requirement, proposal = await blocked_with_proposal(tmp_path)
    review = reviewed(proposal.id, "reject")

    await runtime.submit_event(review)

    decisions = runtime.get_extension_decisions(proposal.id)
    assert decisions[-1].decision.value == "REJECT"
    assert decisions[-1].reviewed_by_event_id == review.id
    assert "rejected by human review" in decisions[-1].reasons
    types = [e.type for e in runtime.event_store.all()]
    assert "extension_rejected" in types
    assert "extension_approved" not in types
    runtime.close()


async def test_an_unknown_decision_word_is_a_rejection(tmp_path):
    """Default deny: anything that is not approval does not proceed."""
    runtime, _requirement, proposal = await blocked_with_proposal(tmp_path)

    await runtime.submit_event(reviewed(proposal.id, "hmm, maybe later"))

    assert runtime.get_extension_proposal(proposal.id).status.value == "REJECTED"
    runtime.close()


async def test_a_second_review_does_not_decide_twice(tmp_path):
    """Exactly one decision converges, however often the event arrives."""
    runtime, _requirement, proposal = await blocked_with_proposal(tmp_path)

    await runtime.submit_event(reviewed(proposal.id, "approve"))
    await runtime.submit_event(reviewed(proposal.id, "reject"))

    decided = runtime.get_extension_proposal(proposal.id)
    assert decided.status is ExtensionProposalStatus.APPROVED
    assert [d.decision.value for d in runtime.get_extension_decisions(proposal.id)] == [
        "REVIEW",
        "APPROVE",
    ]
    runtime.close()
