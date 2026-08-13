"""AT6 (spec §55): a review outlives the Runtime that started it.

Everything needed to finish the action — the proposal, the authorization state,
the validator's continuation and the proposer's continuation — is in SQLite.
The runtime object is discarded entirely and rebuilt from the same file.
"""

from __future__ import annotations

from action_helpers import (
    do_action,
    file_runtime,
    instances_named,
    proposer_instance,
    suspended_validator,
)

from nexus_seed.actions.models import ActionExecutionStatus, ActionProposalStatus
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


async def test_approve_after_restart_resumes_from_the_continuation(tmp_path):
    db_path = tmp_path / "restart.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    file_runtime(runtime, root)
    await runtime.submit_event(
        do_action(
            backend="local_file",
            target="after_restart.txt",
            parameters={"content": "survived a restart"},
            risk_level="HIGH",
        )
    )

    proposal_id = runtime.get_action_proposals()[0].id
    validator_id = suspended_validator(runtime).id
    proposer_id = proposer_instance(runtime).id
    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.REVIEW
    runtime.close()

    # --- everything in memory is thrown away ---
    runtime2 = Runtime(db_path)
    backend2 = file_runtime(runtime2, root)

    # The pending review is discoverable from storage alone.
    assert [p.id for p in runtime2.get_action_proposals(ActionProposalStatus.REVIEW)] == [
        proposal_id
    ]
    assert runtime2.process_store.get_instance(validator_id).status is ProcessStatus.SUSPENDED

    await runtime2.submit_event(
        Event(
            "action_reviewed",
            "human",
            {"proposal_id": str(proposal_id), "decision": "approve"},
        )
    )

    assert runtime2.get_action_proposal(proposal_id).status is ActionProposalStatus.SUCCEEDED
    assert runtime2.process_store.get_instance(validator_id).status is ProcessStatus.COMPLETED
    assert runtime2.process_store.get_instance(proposer_id).status is ProcessStatus.COMPLETED
    assert (root / "after_restart.txt").read_text(encoding="utf-8") == "survived a restart"
    assert len(backend2.calls) == 1

    executions = runtime2.get_action_executions(proposal_id)
    assert [e.status for e in executions] == [ActionExecutionStatus.SUCCEEDED]
    runtime2.close()


async def test_approved_but_unexecuted_action_survives_a_crash(tmp_path):
    """AT10 (spec §59): a crash between APPROVE and execute must not lose or repeat it.

    The runtime is destroyed while the ``action_approved`` event is in flight,
    so no executor ever ran.  On restart the proposal is still APPROVED and can
    be safely driven to completion — exactly once.
    """
    db_path = tmp_path / "crash.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    file_runtime(runtime, root)
    # Register no executor handler: approval happens, execution cannot.
    runtime.registry._handlers.pop("action_executor", None)

    await runtime.submit_event(
        do_action(
            backend="local_file",
            target="pending.txt",
            parameters={"content": "written after recovery"},
            risk_level="LOW",
        )
    )

    proposal_id = runtime.get_action_proposals()[0].id
    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.APPROVED
    assert not (root / "pending.txt").exists()
    # The executor instance exists but failed to find its handler.
    failed_executors = instances_named(runtime, "action_executor")
    assert all(i.status is ProcessStatus.FAILED for i in failed_executors)
    runtime.close()

    # Restart with a working executor and re-deliver the approval.
    runtime2 = Runtime(db_path)
    backend2 = file_runtime(runtime2, root)
    proposal = runtime2.get_action_proposal(proposal_id)
    assert proposal.status is ActionProposalStatus.APPROVED

    await runtime2.submit_event(
        Event(
            "action_approved",
            "recovery",
            {
                "action_proposal_id": str(proposal_id),
                "root_proposal_id": str(proposal.root_proposal_id),
            },
        )
    )

    assert runtime2.get_action_proposal(proposal_id).status is ActionProposalStatus.SUCCEEDED
    assert (root / "pending.txt").read_text(encoding="utf-8") == "written after recovery"
    assert len(backend2.calls) == 1

    # Re-delivering the approval a second time must not write again.
    await runtime2.submit_event(
        Event(
            "action_approved",
            "recovery",
            {
                "action_proposal_id": str(proposal_id),
                "root_proposal_id": str(proposal.root_proposal_id),
            },
        )
    )
    statuses = [e.status for e in runtime2.get_action_executions(proposal_id)]
    assert statuses == [ActionExecutionStatus.SUCCEEDED, ActionExecutionStatus.SKIPPED]
    assert len(backend2.calls) == 1
    runtime2.close()
