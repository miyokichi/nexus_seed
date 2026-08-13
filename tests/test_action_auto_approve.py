"""AT1 (spec §50): a low-risk, permitted action auto-approves and runs once.

The status walk PENDING -> APPROVED -> SUCCEEDED is the whole Phase 3C claim in
miniature: intention, authorization and effect are three separate, durable
steps, and only the last one touches the world.
"""

from __future__ import annotations

from action_helpers import do_action, file_runtime, proposer_instance

from nexus_seed.actions.models import ActionExecutionStatus, ActionProposalStatus
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


async def test_low_risk_action_auto_approves_and_writes_once(tmp_path):
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "auto.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(
            backend="local_file",
            target="result.txt",
            parameters={"content": "analysis completed"},
            risk_level="LOW",
        )
    )

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.SUCCEEDED

    executions = runtime.get_action_executions(proposal.id)
    assert [e.status for e in executions] == [ActionExecutionStatus.SUCCEEDED]
    assert executions[0].attempt == 1

    # The file was written exactly once, with exactly the proposed content.
    written = root / "result.txt"
    assert written.read_text(encoding="utf-8") == "analysis completed"
    assert len([c for c in backend.calls if c.action_type == "write_file"]) == 1

    assert proposer_instance(runtime).status is ProcessStatus.COMPLETED
    runtime.close()


async def test_authorization_precedes_the_side_effect(tmp_path):
    """The approval decision is recorded, and it names the granting definition."""
    runtime = Runtime(tmp_path / "authz.db")
    file_runtime(runtime, tmp_path / "sandbox")

    await runtime.submit_event(
        do_action(backend="local_file", target="ok.txt", risk_level="LOW")
    )

    proposal = runtime.get_action_proposals()[0]
    decisions = runtime.get_action_decisions(proposal.id)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.decision.value == "APPROVE"
    assert decision.process_definition_name == "test_proposer"
    assert decision.granted_permissions == ["filesystem.write"]
    assert decision.mandatory_permissions == ["filesystem.write"]
    runtime.close()
