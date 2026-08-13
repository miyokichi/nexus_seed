"""AT4 + AT5 (spec §53, §54): high risk suspends for a human, then runs.

Human review is not a Runtime feature.  It is an ordinary Continuation waiting
for an ordinary Event (Invariant 18, carried over from Phase 3B) — the same
mechanism a process uses to wait for a measurement.
"""

from __future__ import annotations

from action_helpers import (
    do_action,
    fake_runtime,
    file_runtime,
    instances_named,
    proposer_instance,
    suspended_validator,
)

from nexus_seed.actions.models import ActionExecutionStatus, ActionProposalStatus
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


async def test_high_risk_suspends_with_a_persisted_continuation(tmp_path):
    """AT4: REVIEW -> SUSPENDED, continuation on disk, nothing executed."""
    runtime = Runtime(tmp_path / "review.db")
    backend = fake_runtime(runtime)

    await runtime.submit_event(do_action(target="risky.txt", risk_level="HIGH"))

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REVIEW

    validator = suspended_validator(runtime)
    continuation = runtime.continuation_store.for_instance(validator.id)
    assert continuation.resume_point == "await_action_review"
    assert continuation.waiting_for == {
        "event_type": "action_reviewed",
        "proposal_id": str(proposal.id),
    }

    # No side effect, no execution record, and no executor was even started.
    assert backend.calls == []
    assert runtime.get_action_executions(proposal.id) == []
    assert instances_named(runtime, "action_executor") == []
    runtime.close()


async def test_human_approval_revalidates_then_executes(tmp_path):
    """AT5: approve -> re-validation -> backend runs -> SUCCEEDED."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "approve.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(
            backend="local_file",
            target="reviewed.txt",
            parameters={"content": "approved by a human"},
            risk_level="HIGH",
        )
    )
    proposal_id = runtime.get_action_proposals()[0].id

    await runtime.submit_event(
        Event("action_reviewed", "human", {"proposal_id": str(proposal_id), "decision": "approve"})
    )

    proposal = runtime.get_action_proposal(proposal_id)
    assert proposal.status is ActionProposalStatus.SUCCEEDED
    executions = runtime.get_action_executions(proposal_id)
    assert [e.status for e in executions] == [ActionExecutionStatus.SUCCEEDED]
    assert (root / "reviewed.txt").read_text(encoding="utf-8") == "approved by a human"
    assert proposer_instance(runtime).status is ProcessStatus.COMPLETED

    # Two decisions: the REVIEW that paused it, and the human-driven APPROVE.
    decisions = runtime.get_action_decisions(proposal_id)
    assert [d.decision.value for d in decisions] == ["REVIEW", "APPROVE"]
    assert decisions[0].reviewed_by_event_id is None
    assert decisions[1].reviewed_by_event_id is not None
    runtime.close()


async def test_approval_revalidates_against_current_permissions(tmp_path):
    """A grant withdrawn while the proposal waited must still block it (spec §22)."""
    runtime = Runtime(tmp_path / "revalidate.db")
    backend = fake_runtime(runtime)

    await runtime.submit_event(do_action(target="risky.txt", risk_level="HIGH"))
    proposal_id = runtime.get_action_proposals()[0].id

    # The organisation revokes the proposer's write permission mid-review.
    from action_helpers import proposer_definition, proposer_handler

    runtime.register_process(
        proposer_definition(permissions=("filesystem.read",)), proposer_handler
    )

    await runtime.submit_event(
        Event("action_reviewed", "human", {"proposal_id": str(proposal_id), "decision": "approve"})
    )

    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.REJECTED
    assert backend.calls == []
    runtime.close()


async def test_modify_replaces_the_proposal_and_revalidates_from_scratch(tmp_path):
    """AT (spec §24): a human edit is a new PENDING proposal, never a shortcut."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "modify.db")
    file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(
            backend="local_file",
            target="original.txt",
            parameters={"content": "original"},
            risk_level="HIGH",
        )
    )
    original_id = runtime.get_action_proposals()[0].id

    await runtime.submit_event(
        Event(
            "action_reviewed",
            "human",
            {
                "proposal_id": str(original_id),
                "decision": "modify",
                "replacement": {
                    "backend": "local_file",
                    "action_type": "write_file",
                    "target": "edited.txt",
                    "parameters": {"content": "edited by a human"},
                    "required_permissions": ["filesystem.write"],
                    "declared_side_effects": ["filesystem_write"],
                    "risk_level": "LOW",
                },
            },
        )
    )

    original = runtime.get_action_proposal(original_id)
    assert original.status is ActionProposalStatus.CANCELLED
    assert not (root / "original.txt").exists()

    replacement = [p for p in runtime.get_action_proposals() if p.id != original_id][0]
    assert replacement.replaces_proposal_id == original_id
    # The chain keeps one identity so the original waiter is still attached.
    assert replacement.root_proposal_id == original_id
    assert replacement.status is ActionProposalStatus.SUCCEEDED
    assert (root / "edited.txt").read_text(encoding="utf-8") == "edited by a human"

    # And the process that proposed the original was resumed by the outcome.
    assert proposer_instance(runtime).status is ProcessStatus.COMPLETED
    runtime.close()


async def test_modify_with_an_unusable_replacement_still_refuses_to_act(tmp_path):
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "badmodify.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(backend="local_file", target="x.txt", risk_level="HIGH")
    )
    original_id = runtime.get_action_proposals()[0].id

    await runtime.submit_event(
        Event(
            "action_reviewed",
            "human",
            {
                "proposal_id": str(original_id),
                "decision": "modify",
                "replacement": {"backend": "local_file", "action_type": "delete_everything"},
            },
        )
    )

    replacement = [p for p in runtime.get_action_proposals() if p.id != original_id][0]
    assert replacement.status is ActionProposalStatus.REJECTED
    assert backend.calls == []
    runtime.close()
