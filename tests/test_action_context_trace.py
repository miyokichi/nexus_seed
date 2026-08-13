"""AT14 (spec §63, §47): what was this process looking at when it decided to act?

An ActionProposal keeps the id of the ContextSnapshot compiled for the
activation that produced it.  That makes the *basis* of a decision auditable
after the fact, even once the world has moved on — which is precisely when the
question gets asked.
"""

from __future__ import annotations

from action_helpers import (
    analysis_proposal,
    do_action,
    fake_runtime,
    full_stack,
    human_message,
    instances_named,
)

from nexus_seed.backends import proposal_response
from nexus_seed.core.event import Event
from nexus_seed.runtime.runtime import Runtime


async def test_proposal_keeps_the_context_it_was_decided_on(tmp_path):
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "ctx.db")
    full_stack(runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))])

    await runtime.submit_event(human_message())

    proposal = runtime.get_action_proposals()[0]
    assert proposal.context_snapshot_id is not None

    snapshot = runtime.context_snapshot_store.get(proposal.context_snapshot_id)
    assert snapshot is not None

    worker = instances_named(runtime, "write_analysis_result")[0]
    assert snapshot.process_instance_id == worker.id
    # The snapshot shows the world fact the proposal was built from.
    assert snapshot.context_json["world_state"]["D1_CD"]["analysis_result"]["value"] == (
        "within spec"
    )
    runtime.close()


async def test_the_snapshot_is_reachable_from_the_action_trace(tmp_path):
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "ctxtrace.db")
    full_stack(runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))])

    await runtime.submit_event(human_message())
    proposal = runtime.get_action_proposals()[0]

    trace = runtime.get_action_trace(proposal.id)
    assert trace.context_snapshot is not None
    assert trace.context_snapshot.id == proposal.context_snapshot_id
    runtime.close()


async def test_the_snapshot_is_frozen_even_after_the_world_moves(tmp_path):
    """The audit answer is what was seen *then*, not what is true now."""
    runtime = Runtime(tmp_path / "frozen.db")
    fake_runtime(runtime)

    await runtime.submit_event(do_action(target="risky.txt", risk_level="HIGH"))
    proposal = runtime.get_action_proposals()[0]
    before = runtime.context_snapshot_store.get(proposal.context_snapshot_id)
    compiled_at = before.compiled_at

    # The world changes while the proposal sits in review, and it is approved.
    await runtime.submit_event(
        Event(
            "action_reviewed",
            "human",
            {"proposal_id": str(proposal.id), "decision": "approve"},
        )
    )

    after = runtime.context_snapshot_store.get(proposal.context_snapshot_id)
    assert after.compiled_at == compiled_at
    assert after.context_json == before.context_json
    runtime.close()
