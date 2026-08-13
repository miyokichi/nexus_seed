"""AT18 (spec §68, §85): one real external event converges to one of everything.

This is the test the whole phase exists for.  Four independent idempotency
mechanisms, added in four different phases, have to agree — without having been
merged into one grand mechanism (spec §73):

    Phase 2A  Event.id            a re-submitted event is a no-op
    Phase 2C  work_key            a state version yields work once
    Phase 3C  idempotency_key     an approved action acts once
    Phase 3D  source_event_key    a redelivered occurrence enters once

If they disagree, a webhook that retries three times writes three files.
"""

from __future__ import annotations

from ingress_helpers import (
    ANALYSIS_MESSAGE,
    analysis_proposal,
    full_stack,
    ingress,
    instances_named,
    manual_envelope,
)

from nexus_seed.adapters.file_watch import LocalFileAdapter
from nexus_seed.adapters.webhook import WebhookIngress
from nexus_seed.backends import proposal_response
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


def counts(runtime, root) -> dict:
    """Everything that must stay at one."""
    return {
        "receipts": len(runtime.get_ingress_receipts()),
        "human_message_events": len(runtime.event_store.by_type("human_message")),
        "interpretations": len(runtime.get_proposals()),
        "state_versions": len(runtime.get_state_history("D1_CD", "analysis_result")),
        "work_requirements": len(runtime.get_work_requirements()),
        "work_processes": len(instances_named(runtime, "write_analysis_result")),
        "action_proposals": len(runtime.get_action_proposals()),
        "action_succeeded": len(runtime.event_store.by_type("action_succeeded")),
        "files": len(list(root.glob("*.txt"))),
    }


ALL_ONE = {
    "receipts": 1,
    "human_message_events": 1,
    "interpretations": 1,
    "state_versions": 1,
    "work_requirements": 1,
    "work_processes": 1,
    "action_proposals": 1,
    "action_succeeded": 1,
    "files": 1,
}


async def test_a_redelivered_message_changes_nothing(tmp_path):
    """AT18: deliver the same occurrence three times; the loop runs once."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "dup_loop.db")
    backend = full_stack(
        runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))]
    )
    service = ingress(runtime)

    def envelope():
        return manual_envelope(
            source_event_key="msg-001", payload={"text": ANALYSIS_MESSAGE}
        )

    first = await service.ingest(envelope())
    after_first = counts(runtime, root)
    assert after_first == ALL_ONE

    written = (root / "D1_CD_analysis.txt").read_text(encoding="utf-8")

    for _ in range(2):
        repeat = await service.ingest(envelope())
        assert repeat.duplicate
        assert repeat.event.id == first.event.id

    assert counts(runtime, root) == ALL_ONE
    assert len(backend.calls) == 1
    assert (root / "D1_CD_analysis.txt").read_text(encoding="utf-8") == written
    runtime.close()


async def test_a_webhook_retry_storm_changes_nothing(tmp_path):
    """A provider retrying five times is the normal case, not an attack."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "storm.db")
    backend = full_stack(
        runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))]
    )
    webhook = WebhookIngress(ingress(runtime), token="secret")

    body = {
        "source_event_key": "delivery-1",
        "event_type": "human_message",
        "payload": {"text": ANALYSIS_MESSAGE},
    }
    responses = [await webhook.handle("chat", body, token="secret") for _ in range(5)]
    await webhook.drain_pending()

    # Every delivery is answered successfully; only the first was new.
    assert [r.status_code for r in responses] == [202, 200, 200, 200, 200]
    assert [r.body["duplicate"] for r in responses] == [False, True, True, True, True]
    assert counts(runtime, root) == ALL_ONE
    assert len(backend.calls) == 1
    runtime.close()


async def test_redelivery_after_a_restart_changes_nothing(tmp_path):
    """The convergence is durable, not an in-memory cache."""
    db_path = tmp_path / "dup_restart.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    full_stack(runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))])
    await ingress(runtime).ingest(
        manual_envelope(source_event_key="msg-001", payload={"text": ANALYSIS_MESSAGE})
    )
    assert counts(runtime, root) == ALL_ONE
    runtime.close()

    runtime2 = Runtime(db_path)
    backend2 = full_stack(
        runtime2, root, llm_script=[proposal_response(analysis_proposal(0.95))]
    )
    repeat = await ingress(runtime2).ingest(
        manual_envelope(source_event_key="msg-001", payload={"text": ANALYSIS_MESSAGE})
    )

    assert repeat.duplicate
    assert counts(runtime2, root) == ALL_ONE
    assert backend2.calls == []  # the new runtime's backend was never used
    runtime2.close()


async def test_repeated_polls_of_an_unchanged_file_change_nothing(tmp_path):
    """The pull-side equivalent: re-observing is not re-happening."""
    from nexus_seed.context.requirements import ContextRequirements
    from nexus_seed.core.process import ProcessDefinition

    watched = tmp_path / "watched"
    watched.mkdir()
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "poll_loop.db")
    backend = full_stack(runtime, root)
    adapter = LocalFileAdapter(watched).bind(ingress(runtime))

    async def interpret_file(ctx):
        observation = ctx.observe(
            subject="D1_CD",
            predicate="analysis_completed",
            extracted={"path": ctx.event.payload["path"]},
        )
        delta = ctx.propose_delta(
            entity="D1_CD",
            attribute="analysis_result",
            old_value=None,
            new_value="from file",
            observation=observation,
        )
        return ctx.complete(
            emitted_events=[
                ctx.new_event(
                    "state_delta_created",
                    {
                        "entity": delta.entity,
                        "attribute": delta.attribute,
                        "old_value": delta.old_value,
                        "new_value": delta.new_value,
                        "source_event_id": str(delta.source_event_id),
                        "observation_id": str(observation.id),
                        "state_delta_id": str(delta.id),
                        "confidence": 1.0,
                    },
                )
            ]
        )

    runtime.register_process(
        ProcessDefinition(
            name="interpret_file",
            version="1",
            handler="interpret_file",
            trigger_event_types=("file_created",),
            context_requirements=ContextRequirements(include_trigger_event=True),
        ),
        interpret_file,
    )

    (watched / "report.txt").write_text("done", encoding="utf-8")
    for _ in range(4):
        await adapter.poll_and_ingest()

    assert len(runtime.get_ingress_receipts()) == 1
    assert len(runtime.event_store.by_type("file_created")) == 1
    assert len(runtime.get_work_requirements()) == 1
    assert len(runtime.get_action_proposals()) == 1
    assert len(list(root.glob("*.txt"))) == 1
    assert len(backend.calls) == 1
    assert runtime.get_work_requirements()[0].status is WorkStatus.SATISFIED
    runtime.close()
