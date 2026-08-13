"""AT16 (spec §66): ingress feeds the existing perception pipeline unchanged.

Nothing in Phase 3B knows or cares that its ``human_message`` arrived from a
CLI, a webhook or a file watcher.  That is the test: the same interpretation,
validation, review and world-state machinery runs, with an adapter in front.
"""

from __future__ import annotations

from ingress_helpers import (
    TARGET_MESSAGE,
    full_stack,
    ingress,
    instances_named,
    manual_envelope,
    target_proposal,
)

from nexus_seed.adapters.webhook import WebhookIngress
from nexus_seed.backends import proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime


async def test_a_manual_message_reaches_world_state(tmp_path):
    """AT16: external text in, believed fact out."""
    runtime = Runtime(tmp_path / "perception.db")
    full_stack(runtime, llm_script=[proposal_response(target_proposal(0.95))])

    result = await ingress(runtime).ingest(
        manual_envelope(source_event_key="msg-1", payload={"text": TARGET_MESSAGE})
    )

    assert runtime.state_store.get("D1_CD", "target") == 45

    # The whole chain hangs off the ingested event.
    observation = runtime.observation_store.all()[0]
    assert observation.source_event_id == result.event.id
    proposal = runtime.get_proposals()[0]
    assert proposal.decision.value == "ACCEPT"
    assert proposal.source_event_id == result.event.id

    # And state provenance walks back to an event that knows it came from outside.
    provenance = runtime.get_state_provenance("D1_CD", "target")
    assert provenance.source_event.id == result.event.id
    assert provenance.source_event.is_external
    runtime.close()


async def test_a_webhook_message_reaches_world_state(tmp_path):
    """The same pipeline, a different door."""
    runtime = Runtime(tmp_path / "perception.db")
    full_stack(runtime, llm_script=[proposal_response(target_proposal(0.95))])
    webhook = WebhookIngress(ingress(runtime), token="secret")

    response = await webhook.handle(
        "chat",
        {
            "source_event_key": "delivery-1",
            "event_type": "human_message",
            "payload": {"text": TARGET_MESSAGE},
        },
        token="secret",
    )
    await webhook.drain_pending()

    assert response.status_code == 202
    assert runtime.state_store.get("D1_CD", "target") == 45
    assert runtime.get_ingress_receipts()[0].adapter_id == "chat"
    runtime.close()


async def test_a_low_confidence_reading_still_goes_to_human_review(tmp_path):
    """Ingress does not weaken the perception gate."""
    runtime = Runtime(tmp_path / "review.db")
    full_stack(runtime, llm_script=[proposal_response(target_proposal(0.70))])

    await ingress(runtime).ingest(manual_envelope(payload={"text": TARGET_MESSAGE}))

    interpreter = instances_named(runtime, "interpret_event_llm")[0]
    assert interpreter.status is ProcessStatus.SUSPENDED
    assert runtime.state_store.get("D1_CD", "target") is None

    proposal_id = runtime.get_proposals()[0].id
    await runtime.submit_event(
        Event(
            "interpretation_reviewed",
            "human",
            {"proposal_id": str(proposal_id), "decision": "approve"},
        )
    )
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()


async def test_a_file_change_can_drive_perception_too(tmp_path):
    """A file event is an ordinary event; a process decides what it means."""
    from nexus_seed.adapters.file_watch import LocalFileAdapter
    from nexus_seed.context.requirements import ContextRequirements
    from nexus_seed.core.process import ProcessDefinition

    watched = tmp_path / "watched"
    watched.mkdir()
    runtime = Runtime(tmp_path / "file_perception.db")
    service = ingress(runtime)
    adapter = LocalFileAdapter(watched).bind(service)

    # A process — not the adapter — turns "a file changed" into a reading.
    async def note_file(ctx):
        ctx.observe(
            subject=ctx.event.payload["path"],
            predicate="file_changed",
            extracted={"content_hash": ctx.event.payload["content_hash"]},
        )
        return ctx.complete(output={"noted": ctx.event.payload["path"]})

    runtime.register_process(
        ProcessDefinition(
            name="note_file",
            version="1",
            handler="note_file",
            trigger_event_types=("file_created", "file_modified"),
            context_requirements=ContextRequirements(include_trigger_event=True),
        ),
        note_file,
    )

    (watched / "report.txt").write_text("v1", encoding="utf-8")
    await adapter.poll_and_ingest()

    observations = runtime.observation_store.all()
    assert [o.subject for o in observations] == ["report.txt"]
    assert observations[0].predicate == "file_changed"
    # The observation traces back to an externally-originated event.
    source = runtime.event_store.get(observations[0].source_event_id)
    assert source.is_external
    assert runtime.get_ingress_receipt_for_event(source.id).adapter_id == "local_file"
    runtime.close()
