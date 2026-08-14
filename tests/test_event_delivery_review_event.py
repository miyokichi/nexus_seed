"""AT14 (spec §65, §40): a human's decision is never thrown away.

Of everything the system stores, an approval is the one it has least right to
lose: a person looked at something and said yes.  If a crash between "approval
recorded" and "approval routed" discarded it, the work would sit suspended
forever and the human would have no way to know.
"""

from __future__ import annotations

from action_helpers import do_action, fake_runtime, file_runtime, proposer_instance

from nexus_seed.actions.models import ActionProposalStatus
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


def approval(proposal_id, decision="approve") -> Event:
    return Event(
        "action_reviewed",
        "alex@example.test",
        {"proposal_id": str(proposal_id), "decision": decision},
    )


async def test_an_approval_survives_a_crash_before_it_is_routed(tmp_path):
    """AT14."""
    db_path = tmp_path / "review.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    file_runtime(runtime, root)
    await runtime.submit_event(
        do_action(backend="local_file", target="reviewed.txt", risk_level="HIGH")
    )
    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REVIEW

    # The human approves; the runtime dies before the event is routed.
    event = approval(proposal.id)
    runtime.event_store.append(event)
    assert runtime.get_event_delivery(event.id).status.value == "PENDING"
    assert not (root / "reviewed.txt").exists()
    runtime.close()

    # --- restart ---
    runtime2 = Runtime(db_path)
    backend2 = file_runtime(runtime2, root)
    assert runtime2.get_pending_event_delivery_count() == 1

    await runtime2.run_pending()

    assert runtime2.get_event_delivery(event.id).status.value == "DELIVERED"
    assert runtime2.get_action_proposal(proposal.id).status is ActionProposalStatus.SUCCEEDED
    assert (root / "reviewed.txt").exists()
    assert len(backend2.calls) == 1
    runtime2.close()


async def test_a_rejection_survives_the_same_crash(tmp_path):
    """A refusal is a decision too, and must not silently become a hang."""
    db_path = tmp_path / "review.db"

    runtime = Runtime(db_path)
    fake_runtime(runtime)
    await runtime.submit_event(do_action(target="risky.txt", risk_level="HIGH"))
    proposal = runtime.get_action_proposals()[0]

    event = approval(proposal.id, "reject")
    runtime.event_store.append(event)
    runtime.close()

    runtime2 = Runtime(db_path)
    backend2 = fake_runtime(runtime2)
    await runtime2.run_pending()

    assert runtime2.get_action_proposal(proposal.id).status is ActionProposalStatus.REJECTED
    assert backend2.calls == []
    assert proposer_instance(runtime2).status is ProcessStatus.COMPLETED
    runtime2.close()


async def test_an_llm_interpretation_review_is_covered_too(tmp_path):
    """The same guarantee for the Phase 3B review path."""
    from ingress_helpers import TARGET_MESSAGE, full_stack, target_proposal

    from nexus_seed.backends import proposal_response

    db_path = tmp_path / "interp.db"

    runtime = Runtime(db_path)
    full_stack(runtime, llm_script=[proposal_response(target_proposal(0.70))])
    await runtime.submit_event(Event("human_message", "user", {"text": TARGET_MESSAGE}))
    proposal = runtime.get_proposals()[0]
    assert runtime.state_store.get("D1_CD", "target") is None

    reviewed = Event(
        "interpretation_reviewed",
        "human",
        {"proposal_id": str(proposal.id), "decision": "approve"},
    )
    runtime.event_store.append(reviewed)
    runtime.close()

    runtime2 = Runtime(db_path)
    full_stack(runtime2)
    await runtime2.run_pending()

    assert runtime2.get_event_delivery(reviewed.id).status.value == "DELIVERED"
    assert runtime2.state_store.get("D1_CD", "target") == 45
    runtime2.close()


async def test_a_re_delivered_approval_does_not_act_twice(tmp_path):
    """Delivery is at-least-once; approving is once."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "review.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(backend="local_file", target="reviewed.txt", risk_level="HIGH")
    )
    proposal = runtime.get_action_proposals()[0]
    await runtime.submit_event(approval(proposal.id))
    calls_before = len(backend.calls)

    reviewed = runtime.event_store.by_type("action_reviewed")[0]
    runtime.db.execute(
        "UPDATE event_deliveries SET status = 'PENDING' WHERE event_id = ?",
        (str(reviewed.id),),
    )
    await runtime.run_pending()

    assert len(backend.calls) == calls_before == 1
    assert len(runtime.event_store.by_type("action_succeeded")) == 1
    runtime.close()
