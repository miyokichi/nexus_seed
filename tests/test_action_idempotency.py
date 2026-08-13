"""AT9 (spec §58): one proposal, at most one external effect.

Phase 3C does **not** attempt distributed exactly-once (spec §36).  What it
guarantees is *at-most-once per idempotency key*, enforced twice over:

* in NEXUS SEED — a SUCCEEDED ActionExecution for the key means the backend is
  not called again;
* in the backend — a durable journal answers a repeated key without redoing the
  effect, which covers the crash window between "the world changed" and "the
  database committed".
"""

from __future__ import annotations

from action_helpers import do_action, fake_runtime, file_runtime, instances_named

from nexus_seed.actions.models import ActionExecutionStatus, ActionProposalStatus
from nexus_seed.backends import ActionRequest, LocalFileActionBackend
from nexus_seed.core.event import Event
from nexus_seed.runtime.runtime import Runtime


def approved_event(proposal) -> Event:
    return Event(
        "action_approved",
        "test",
        {
            "action_proposal_id": str(proposal.id),
            "root_proposal_id": str(proposal.root_proposal_id),
        },
    )


async def test_duplicate_activation_produces_one_effect(tmp_path):
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "idem.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(
            backend="local_file", target="once.txt", parameters={"content": "first"}
        )
    )
    proposal = runtime.get_action_proposals()[0]
    assert (root / "once.txt").read_text(encoding="utf-8") == "first"

    # Re-deliver the approval twice more, as a duplicate activation would.
    await runtime.submit_event(approved_event(proposal))
    await runtime.submit_event(approved_event(proposal))

    statuses = [e.status for e in runtime.get_action_executions(proposal.id)]
    assert statuses == [
        ActionExecutionStatus.SUCCEEDED,
        ActionExecutionStatus.SKIPPED,
        ActionExecutionStatus.SKIPPED,
    ]
    # The backend was called exactly once, and the file was written once.
    assert len(backend.calls) == 1
    assert (root / "once.txt").read_text(encoding="utf-8") == "first"

    # Exactly one action_succeeded event exists for the whole chain.
    assert len(runtime.event_store.by_type("action_succeeded")) == 1

    # The skipped attempts name the execution they deferred to.
    skipped = runtime.get_action_executions(proposal.id)[1]
    assert skipped.result["duplicate_of"] == str(
        runtime.get_action_executions(proposal.id)[0].id
    )
    runtime.close()


async def test_skipped_attempts_do_not_change_the_final_status(tmp_path):
    runtime = Runtime(tmp_path / "idem2.db")
    fake_runtime(runtime)

    await runtime.submit_event(do_action(target="x.txt"))
    proposal = runtime.get_action_proposals()[0]
    await runtime.submit_event(approved_event(proposal))

    assert runtime.get_action_proposal(proposal.id).status is ActionProposalStatus.SUCCEEDED
    assert len(instances_named(runtime, "action_executor")) == 2
    runtime.close()


async def test_distinct_proposals_for_the_same_target_both_act(tmp_path):
    """Idempotency is per-intention: a genuinely new proposal may act again."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "twice.db")
    backend = file_runtime(runtime, root)

    await runtime.submit_event(
        do_action(backend="local_file", target="log.txt", parameters={"content": "one"})
    )
    await runtime.submit_event(
        do_action(backend="local_file", target="log.txt", parameters={"content": "two"})
    )

    assert len(runtime.get_action_proposals()) == 2
    assert len(backend.calls) == 2
    assert (root / "log.txt").read_text(encoding="utf-8") == "two"
    runtime.close()


async def test_backend_journal_covers_the_crash_window(tmp_path):
    """The effect landed but the DB never learned: a replay must not repeat it.

    Simulates spec §35 directly against the backend, with a *fresh* backend
    object reading the on-disk journal — the state NEXUS SEED would be in after
    a crash between the write and the commit.
    """
    root = tmp_path / "sandbox"
    backend = LocalFileActionBackend(root)
    request = ActionRequest(
        action_type="write_file",
        target="crash.txt",
        parameters={"content": "written before the crash"},
        idempotency_key="proposal-1:local_file:write_file:crash.txt",
    )
    first = await backend.execute(request)
    assert first.success and not first.duplicate

    # The file is edited out-of-band; a genuine re-execution would restore it.
    (root / "crash.txt").write_text("touched by someone else", encoding="utf-8")

    replay = LocalFileActionBackend(root)
    second = await replay.execute(request)
    assert second.success and second.duplicate
    assert second.output["path"] == first.output["path"]
    # Nothing was rewritten: the journal answered instead of the filesystem.
    assert (root / "crash.txt").read_text(encoding="utf-8") == "touched by someone else"
