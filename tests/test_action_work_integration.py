"""AT16 + AT17 (spec §65, §66): the closed loop, end to end.

    Raw Event -> LLM Interpretation -> World State -> WorkRequirement
      -> action-capable Process -> Context compile -> ActionProposal
        -> Permission / Risk Policy -> ExecutionBackend -> file write
          -> ActionExecution -> action_succeeded -> Process Complete
            -> WorkRequirement SATISFIED

Nothing in that chain is new machinery: perception, work and action are three
uses of the same Event / Process / Continuation primitives.
"""

from __future__ import annotations

from action_helpers import (
    analysis_proposal,
    full_stack,
    human_message,
    instances_named,
)

from nexus_seed.actions import (
    ActionDecision,
    ActionExecutionStatus,
    ActionPolicy,
    ActionProposalStatus,
    RiskLevel,
)
from nexus_seed.backends import proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


async def test_message_in_file_out(tmp_path):
    """AT16: one natural-language message ends as a file on disk."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "e2e.db")
    backend = full_stack(
        runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))]
    )

    await runtime.submit_event(human_message())

    # --- perception reached world state ---
    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"

    # --- work intelligence derived and spawned the outward-acting work ---
    requirement = runtime.get_work_requirements()[0]
    assert requirement.work_type == "write_analysis_result"
    assert requirement.status is WorkStatus.SATISFIED

    worker = instances_named(runtime, "write_analysis_result")[0]
    assert worker.status is ProcessStatus.COMPLETED
    assert worker.work_requirement_id == requirement.id

    # --- the action boundary authorized and performed it ---
    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.SUCCEEDED
    assert proposal.created_by_process_id == worker.id
    assert proposal.source_work_requirement_id == requirement.id
    assert proposal.action_type == "write_file"

    executions = runtime.get_action_executions(proposal.id)
    assert [e.status for e in executions] == [ActionExecutionStatus.SUCCEEDED]

    # --- the world actually changed, once ---
    written = (root / "D1_CD_analysis.txt").read_text(encoding="utf-8")
    assert "analysis=within spec" in written
    assert len(backend.calls) == 1

    # --- and the result came back in as an Event ---
    succeeded = runtime.event_store.by_type("action_succeeded")
    assert len(succeeded) == 1
    assert succeeded[0].payload["action_execution_id"] == str(executions[0].id)
    runtime.close()


REVIEW_EVERYTHING = ActionPolicy(
    decisions={
        RiskLevel.LOW: ActionDecision.REVIEW,
        RiskLevel.MEDIUM: ActionDecision.REVIEW,
        RiskLevel.HIGH: ActionDecision.REVIEW,
        RiskLevel.CRITICAL: ActionDecision.REJECT,
    }
)


async def test_review_end_to_end_across_a_restart(tmp_path):
    """AT17: the same work, under a policy that lets no file be written unattended.

    Only the policy differs from :func:`test_message_in_file_out` — the work,
    the process and the proposal are identical.  That is the point: how much
    autonomy an action gets is configuration, not code.
    """
    db_path = tmp_path / "review_e2e.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    full_stack(
        runtime,
        root,
        llm_script=[proposal_response(analysis_proposal(0.95))],
        policy=REVIEW_EVERYTHING,
    )
    await runtime.submit_event(human_message())

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REVIEW
    worker = instances_named(runtime, "write_analysis_result")[0]
    assert worker.status is ProcessStatus.SUSPENDED
    requirement = runtime.get_work_requirements()[0]
    assert requirement.status is WorkStatus.SPAWNED
    assert not (root / "D1_CD_analysis.txt").exists()
    runtime.close()

    # --- runtime destroyed and rebuilt from SQLite ---
    runtime2 = Runtime(db_path)
    backend2 = full_stack(runtime2, root, policy=REVIEW_EVERYTHING)

    await runtime2.submit_event(
        Event(
            "action_reviewed",
            "human",
            {"proposal_id": str(proposal.id), "decision": "approve"},
        )
    )

    assert runtime2.get_action_proposal(proposal.id).status is ActionProposalStatus.SUCCEEDED
    assert runtime2.process_store.get_instance(worker.id).status is ProcessStatus.COMPLETED
    assert runtime2.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
    assert (root / "D1_CD_analysis.txt").exists()
    assert len(backend2.calls) == 1

    # Both suspensions (validator waiting for a human, worker waiting for the
    # outcome) were released and no continuation is left dangling.
    assert runtime2.continuation_store.all() == []
    runtime2.close()


async def test_low_confidence_interpretation_never_reaches_an_action(tmp_path):
    """A reading that fails the perception gate cannot become an act."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "lowconf.db")
    backend = full_stack(
        runtime, root, llm_script=[proposal_response(analysis_proposal(0.70))]
    )

    await runtime.submit_event(human_message())

    assert runtime.state_store.get("D1_CD", "analysis_result") is None
    assert runtime.get_work_requirements() == []
    assert runtime.get_action_proposals() == []
    assert backend.calls == []
    runtime.close()
