"""AT2 + AT3 (spec §51, §52): permission denial and escalation refusal.

Two distinct attacks on the boundary:

* the process simply *isn't allowed* to do what it asked for;
* the process asks honestly-looking but under-declares what the action needs,
  hoping the declared list is the only thing checked.

Both must end at REJECTED with the backend never called.
"""

from __future__ import annotations

from action_helpers import do_action, fake_runtime, proposer_instance

from nexus_seed.actions.models import ActionProposalStatus
from nexus_seed.actions.permissions import granted_permissions, missing_permissions
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.runtime.runtime import Runtime


def test_permissions_come_from_the_definition_and_default_to_none():
    granted = ProcessDefinition(
        name="x", version="1", handler="x", metadata={"permissions": ["filesystem.read"]}
    )
    assert granted_permissions(granted) == ["filesystem.read"]
    # No metadata at all -> granted nothing (default deny).
    assert granted_permissions(ProcessDefinition(name="x", version="1", handler="x")) == []
    assert granted_permissions(None) == []
    # Garbage metadata is not a grant either.
    assert granted_permissions(
        ProcessDefinition(name="x", version="1", handler="x", metadata={"permissions": "all"})
    ) == []


def test_missing_permissions_preserves_order_and_dedupes():
    assert missing_permissions(
        ["a", "b", "a", "c"], ["b"]
    ) == ["a", "c"]


async def test_action_is_rejected_when_the_process_lacks_the_permission(tmp_path):
    """AT2: granted filesystem.read, proposal requires filesystem.write."""
    runtime = Runtime(tmp_path / "denied.db")
    backend = fake_runtime(runtime, permissions=("filesystem.read",))

    await runtime.submit_event(
        do_action(target="denied.txt", required_permissions=["filesystem.write"])
    )

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REJECTED

    # The backend was never reached and nothing was journalled as executed.
    assert backend.calls == []
    assert backend.effect_count == 0
    assert runtime.get_action_executions(proposal.id) == []

    # The refusal is auditable and names what was missing.
    decision = runtime.get_action_decisions(proposal.id)[0]
    assert decision.decision.value == "REJECT"
    assert decision.granted_permissions == ["filesystem.read"]
    assert any("filesystem.write" in reason for reason in decision.reasons)

    # The proposing process was woken by the refusal rather than left hanging.
    proposer = proposer_instance(runtime)
    assert proposer.status is ProcessStatus.COMPLETED
    assert proposer.local_state["output"]["outcome"] == "action_rejected"
    runtime.close()


async def test_under_declared_permissions_cannot_escalate(tmp_path):
    """AT3: the proposal claims it needs nothing; write_file still needs write."""
    runtime = Runtime(tmp_path / "escalate.db")
    backend = fake_runtime(runtime, permissions=("filesystem.write",))

    await runtime.submit_event(
        do_action(target="sneaky.txt", required_permissions=[])
    )

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REJECTED
    assert backend.calls == []

    decision = runtime.get_action_decisions(proposal.id)[0]
    # The backend's mandatory permission is what caught it, not the declaration.
    assert decision.required_permissions == []
    assert decision.mandatory_permissions == ["filesystem.write"]
    assert any("under-declares" in reason for reason in decision.reasons)
    runtime.close()


async def test_declared_permission_beyond_the_grant_is_also_refused(tmp_path):
    """Declaring more than granted fails even if the backend mandates less."""
    runtime = Runtime(tmp_path / "extra.db")
    backend = fake_runtime(runtime, permissions=("filesystem.write",))

    await runtime.submit_event(
        do_action(
            action_type="noop",
            target="x.txt",
            required_permissions=["shell.execute"],
        )
    )

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REJECTED
    assert backend.calls == []
    runtime.close()
