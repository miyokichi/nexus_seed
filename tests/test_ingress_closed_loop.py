"""AT17 + AT19 (spec §67, §69): the whole loop, from world back to world.

    External input -> Ingress -> Raw Event -> Interpretation -> World State
      -> WorkRequirement -> Work Process -> ActionProposal -> Policy
        -> LocalFileActionBackend -> a real file -> action_succeeded
          -> WorkRequirement SATISFIED

Every stage already existed except the first.  What this proves is that adding
the ingress boundary did not require any of the others to change.
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

from nexus_seed.actions import ActionDecision, ActionPolicy, ActionProposalStatus, RiskLevel
from nexus_seed.adapters.file_watch import LocalFileAdapter
from nexus_seed.adapters.webhook import WebhookIngress
from nexus_seed.backends import proposal_response
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


async def test_one_external_message_ends_as_one_file(tmp_path):
    """AT17."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "loop.db")
    backend = full_stack(
        runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))]
    )

    result = await ingress(runtime).ingest(
        manual_envelope(source_event_key="msg-001", payload={"text": ANALYSIS_MESSAGE})
    )

    # --- ingress ---
    receipt = runtime.get_ingress_receipt_for_event(result.event.id)
    assert receipt.source_event_key == "msg-001"

    # --- perception ---
    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"

    # --- work ---
    requirement = runtime.get_work_requirements()[0]
    assert requirement.work_type == "write_analysis_result"
    assert requirement.status is WorkStatus.SATISFIED
    worker = instances_named(runtime, "write_analysis_result")[0]
    assert worker.status is ProcessStatus.COMPLETED

    # --- action ---
    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.SUCCEEDED
    assert len(runtime.get_action_executions(proposal.id)) == 1

    # --- the world actually changed, once ---
    assert "analysis=within spec" in (root / "D1_CD_analysis.txt").read_text(encoding="utf-8")
    assert len(backend.calls) == 1
    assert len(runtime.event_store.by_type("action_succeeded")) == 1

    # --- and it is all one traceable chain ---
    trace = runtime.get_ingress_trace(result.event.id)
    assert trace.source_identity == ("manual", "msg-001")
    assert trace.reached_the_world
    runtime.close()


async def test_a_webhook_drives_the_same_loop(tmp_path):
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "loop.db")
    backend = full_stack(
        runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))]
    )
    webhook = WebhookIngress(ingress(runtime), token="secret")

    response = await webhook.handle(
        "chat",
        {
            "source_event_key": "delivery-1",
            "event_type": "human_message",
            "payload": {"text": ANALYSIS_MESSAGE},
        },
        token="secret",
    )
    await webhook.drain_pending()

    assert response.status_code == 202
    assert (root / "D1_CD_analysis.txt").exists()
    assert runtime.get_work_requirements()[0].status is WorkStatus.SATISFIED
    assert len(backend.calls) == 1
    runtime.close()


async def test_a_file_change_drives_the_loop_to_another_file(tmp_path):
    """Observed a file, acted on the world, wrote a file — outside to outside."""
    from nexus_seed.context.requirements import ContextRequirements
    from nexus_seed.core.process import ProcessDefinition

    watched = tmp_path / "watched"
    watched.mkdir()
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "fileloop.db")
    backend = full_stack(runtime, out)
    service = ingress(runtime)
    adapter = LocalFileAdapter(watched).bind(service)

    # A process interprets the file event into a world fact; the adapter did not.
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
            reason="a watched report file appeared",
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

    (watched / "report.txt").write_text("analysis done", encoding="utf-8")
    await adapter.poll_and_ingest()

    assert runtime.state_store.get("D1_CD", "analysis_result") == "from file"
    assert runtime.get_work_requirements()[0].status is WorkStatus.SATISFIED
    assert (out / "D1_CD_analysis.txt").exists()
    assert len(backend.calls) == 1
    runtime.close()


REVIEW_EVERYTHING = ActionPolicy(
    decisions={
        RiskLevel.LOW: ActionDecision.REVIEW,
        RiskLevel.MEDIUM: ActionDecision.REVIEW,
        RiskLevel.HIGH: ActionDecision.REVIEW,
        RiskLevel.CRITICAL: ActionDecision.REJECT,
    }
)


async def test_the_loop_survives_a_restart_during_action_review(tmp_path):
    """AT19: stop the world mid-loop, rebuild from SQLite, finish correctly."""
    db_path = tmp_path / "restart_loop.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    full_stack(
        runtime,
        root,
        llm_script=[proposal_response(analysis_proposal(0.95))],
        policy=REVIEW_EVERYTHING,
    )
    result = await ingress(runtime).ingest(
        manual_envelope(source_event_key="msg-001", payload={"text": ANALYSIS_MESSAGE})
    )

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status is ActionProposalStatus.REVIEW
    worker = instances_named(runtime, "write_analysis_result")[0]
    assert worker.status is ProcessStatus.SUSPENDED
    assert not (root / "D1_CD_analysis.txt").exists()
    runtime.close()

    # --- runtime destroyed and rebuilt ---
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
    assert runtime2.get_work_requirements()[0].status is WorkStatus.SATISFIED
    assert (root / "D1_CD_analysis.txt").exists()
    assert len(backend2.calls) == 1
    assert runtime2.continuation_store.all() == []

    # The redelivery that a restart often triggers is still a no-op.
    redelivery = await ingress(runtime2).ingest(
        manual_envelope(source_event_key="msg-001", payload={"text": ANALYSIS_MESSAGE})
    )
    assert redelivery.duplicate
    assert redelivery.event.id == result.event.id
    assert len(backend2.calls) == 1
    runtime2.close()


async def test_the_loop_survives_a_restart_right_after_ingress(tmp_path):
    """The event is durable *and owed a routing attempt* before anything runs.

    Phase 3F: the crash happens after the event committed but before it was
    routed.  The restart finds the outstanding delivery and completes the loop
    — no re-ingest, and no reliance on the source redelivering.
    """
    db_path = tmp_path / "early_restart.db"
    root = tmp_path / "sandbox"

    runtime = Runtime(db_path)
    # deliver=False: persisted, obligation recorded, nothing routed yet.
    result = await ingress(runtime).ingest(
        manual_envelope(source_event_key="msg-001", payload={"text": ANALYSIS_MESSAGE}),
        deliver=False,
    )
    assert runtime.process_store.all_instances() == []
    assert runtime.get_event_delivery(result.event.id).status.value == "PENDING"
    runtime.close()

    runtime2 = Runtime(db_path)
    backend2 = full_stack(
        runtime2, root, llm_script=[proposal_response(analysis_proposal(0.95))]
    )
    # The outstanding delivery is still owed, and the sweep pays it.  (The
    # rebuilt runtime also announces the capabilities it introduces, so the
    # ingress event is not the only thing outstanding.)
    assert runtime2.get_event_delivery(result.event.id).status.value == "PENDING"
    await runtime2.run_pending()

    assert runtime2.get_event_delivery(result.event.id).status.value == "DELIVERED"
    assert runtime2.state_store.get("D1_CD", "analysis_result") == "within spec"
    assert (root / "D1_CD_analysis.txt").exists()
    assert len(backend2.calls) == 1
    assert len(runtime2.event_store.by_type("human_message")) == 1
    assert runtime2.get_pending_event_delivery_count() == 0
    runtime2.close()
