"""AT18 (spec §110): a proposal waiting for a person survives the runtime.

Human review is an ordinary Continuation (Invariant 18), so this is the same
property Phase 3B and 3C already have — but it matters more here.  A proposal
about changing the system is exactly the kind of thing that sits unanswered for
days, across restarts and deployments, and it must converge on **exactly one**
decision when the answer finally arrives.
"""

from __future__ import annotations

from extension_helpers import (
    POWERPOINT,
    ExtensionProposalStatus,
    block,
    extension_runtime,
    gap_work,
    needs,
    only_gap,
    only_proposal,
    reopen,
    reviewed,
)


async def blocked_and_waiting(tmp_path):
    """Reach a proposal in REVIEW, then throw the runtime away."""
    runtime = extension_runtime(tmp_path)
    requirement = gap_work(runtime, required=[needs("parse_powerpoint", POWERPOINT)])
    await block(runtime, requirement)
    proposal = only_proposal(runtime)
    assert proposal.status is ExtensionProposalStatus.REVIEW
    gap_id = only_gap(runtime).id
    runtime.close()
    return requirement, proposal, gap_id


async def test_the_review_survives_a_restart_and_approves(tmp_path):
    requirement, proposal, gap_id = await blocked_and_waiting(tmp_path)

    rebuilt = reopen(tmp_path)
    assert rebuilt.get_extension_proposal(proposal.id).status is (
        ExtensionProposalStatus.REVIEW
    )
    await rebuilt.submit_event(reviewed(proposal.id, "approve"))

    decided = rebuilt.get_extension_proposal(proposal.id)
    assert decided.status is ExtensionProposalStatus.APPROVED
    # Exactly one logical decision, from exactly one resumed instance.
    decisions = rebuilt.get_extension_decisions(proposal.id)
    assert [d.decision.value for d in decisions] == ["REVIEW", "APPROVE"]
    assert rebuilt.get_capability_gap(gap_id).status.value == "PROPOSAL_APPROVED"
    rebuilt.close()


async def test_the_review_survives_a_restart_and_rejects(tmp_path):
    requirement, proposal, gap_id = await blocked_and_waiting(tmp_path)

    rebuilt = reopen(tmp_path)
    await rebuilt.submit_event(reviewed(proposal.id, "reject"))

    assert rebuilt.get_extension_proposal(proposal.id).status is (
        ExtensionProposalStatus.REJECTED
    )
    assert rebuilt.get_capability_gap(gap_id).status.value == "OPEN"
    rebuilt.close()


async def test_the_continuation_is_rebuilt_not_replayed(tmp_path):
    """One instance across the restart — nothing is analysed a second time."""
    requirement, proposal, _gap_id = await blocked_and_waiting(tmp_path)

    rebuilt = reopen(tmp_path)
    before = [
        i
        for i in rebuilt.process_store.all_instances()
        if i.definition_name == "analyze_capability_gap"
    ]
    assert len(before) == 1
    assert rebuilt.continuation_store.for_instance(before[0].id) is not None

    await rebuilt.submit_event(reviewed(proposal.id, "approve"))

    after = [
        i
        for i in rebuilt.process_store.all_instances()
        if i.definition_name == "analyze_capability_gap"
    ]
    assert len(after) == 1
    assert after[0].id == before[0].id
    assert rebuilt.continuation_store.for_instance(after[0].id) is None
    assert len(rebuilt.get_extension_proposals()) == 1
    rebuilt.close()


async def test_a_review_delivered_twice_across_a_restart_decides_once(tmp_path):
    """At-least-once delivery, exactly-one decision (spec §84)."""
    requirement, proposal, _gap_id = await blocked_and_waiting(tmp_path)

    rebuilt = reopen(tmp_path)
    approval = reviewed(proposal.id, "approve")
    await rebuilt.submit_event(approval)
    rebuilt.close()

    again = reopen(tmp_path)
    # A different event carrying the same human answer.
    await again.submit_event(reviewed(proposal.id, "approve"))

    decisions = again.get_extension_decisions(proposal.id)
    assert [d.decision.value for d in decisions] == ["REVIEW", "APPROVE"]
    assert again.get_extension_proposal(proposal.id).status.value == "APPROVED"
    again.close()


async def test_nothing_is_left_pending_after_the_decision(tmp_path):
    requirement, proposal, _gap_id = await blocked_and_waiting(tmp_path)

    rebuilt = reopen(tmp_path)
    await rebuilt.submit_event(reviewed(proposal.id, "approve"))

    assert rebuilt.get_pending_event_delivery_count() == 0
    assert rebuilt.get_failed_event_deliveries() == []
    rebuilt.close()
