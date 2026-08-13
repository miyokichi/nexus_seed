"""AT7 (spec §56): a human refusal ends the action cleanly.

Refusal is a *normal* outcome, not an error: the proposal is REJECTED, no
backend is touched, and the process that wanted to act completes rather than
failing or hanging.
"""

from __future__ import annotations

from action_helpers import do_action, fake_runtime, instances_named, proposer_instance

from nexus_seed.actions.models import ActionProposalStatus, RiskLevel
from nexus_seed.actions.policy import ActionPolicy
from nexus_seed.actions.models import ActionDecision
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


async def test_human_reject_produces_no_side_effect(tmp_path):
    runtime = Runtime(tmp_path / "reject.db")
    backend = fake_runtime(runtime)

    await runtime.submit_event(do_action(target="nope.txt", risk_level="HIGH"))
    proposal_id = runtime.get_action_proposals()[0].id

    await runtime.submit_event(
        Event(
            "action_reviewed",
            "human",
            {"proposal_id": str(proposal_id), "decision": "reject"},
        )
    )

    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.REJECTED
    assert backend.calls == []
    assert backend.effect_count == 0
    assert runtime.get_action_executions(proposal_id) == []
    assert instances_named(runtime, "action_executor") == []

    # Both the validator and the proposer terminate normally.
    validator = instances_named(runtime, "action_validator")[0]
    assert validator.status is ProcessStatus.COMPLETED
    proposer = proposer_instance(runtime)
    assert proposer.status is ProcessStatus.COMPLETED
    assert proposer.local_state["output"]["outcome"] == "action_rejected"

    decision = runtime.get_action_decisions(proposal_id)[-1]
    assert decision.decision.value == "REJECT"
    assert decision.reasons == ["rejected by human review"]
    runtime.close()


async def test_an_unrecognised_review_decision_is_treated_as_reject(tmp_path):
    """Anything that is not an explicit approval must not let the action run."""
    runtime = Runtime(tmp_path / "unknown.db")
    backend = fake_runtime(runtime)

    await runtime.submit_event(do_action(target="nope.txt", risk_level="HIGH"))
    proposal_id = runtime.get_action_proposals()[0].id

    await runtime.submit_event(
        Event("action_reviewed", "human", {"proposal_id": str(proposal_id), "decision": "hmm"})
    )

    assert runtime.get_action_proposal(proposal_id).status is ActionProposalStatus.REJECTED
    assert backend.calls == []
    runtime.close()


async def test_critical_risk_is_rejected_by_policy_without_a_human(tmp_path):
    """A CRITICAL action never even reaches review under the default policy."""
    runtime = Runtime(tmp_path / "critical.db")
    backend = fake_runtime(runtime)

    await runtime.submit_event(do_action(target="boom.txt", risk_level="CRITICAL"))

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REJECTED
    assert backend.calls == []
    assert runtime.get_action_decisions(proposal.id)[0].risk_level is RiskLevel.CRITICAL
    runtime.close()


async def test_a_stricter_policy_changes_the_outcome_without_code_changes(tmp_path):
    """The same MEDIUM proposal reviews instead of approving, by configuration."""
    strict = ActionPolicy(
        decisions={
            RiskLevel.LOW: ActionDecision.APPROVE,
            RiskLevel.MEDIUM: ActionDecision.REVIEW,
            RiskLevel.HIGH: ActionDecision.REJECT,
            RiskLevel.CRITICAL: ActionDecision.REJECT,
        }
    )
    runtime = Runtime(tmp_path / "strict.db")
    backend = fake_runtime(runtime, policy=strict)

    await runtime.submit_event(do_action(target="medium.txt", risk_level="MEDIUM"))

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REVIEW
    assert backend.calls == []
    # The policy that produced the decision is recorded alongside it.
    assert runtime.get_action_decisions(proposal.id)[0].policy["MEDIUM"] == "REVIEW"
    runtime.close()
