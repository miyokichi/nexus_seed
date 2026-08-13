"""AT15 (spec §65, §41-§43): from a NEXUS Event back out to the world.

Phase 3C could answer "why did we act?" as far back as a raw Event.  With
ingress, the chain reaches the occurrence itself — and the question can be
asked from either end: given our event id, or given only the provider's
delivery id.
"""

from __future__ import annotations

from ingress_helpers import (
    analysis_proposal,
    full_stack,
    ingress,
    manual_envelope,
)

from nexus_seed.backends import proposal_response
from nexus_seed.runtime.runtime import Runtime


async def test_an_event_names_the_receipt_that_produced_it(tmp_path):
    runtime = Runtime(tmp_path / "trace.db")
    result = await ingress(runtime).ingest(manual_envelope(source_event_key="demo-001"))

    event = runtime.event_store.get(result.event.id)
    receipt = runtime.get_ingress_receipt_for_event(event.id)

    assert event.ingress_receipt_id == receipt.id
    assert receipt.adapter_id == "manual"
    assert receipt.source_event_key == "demo-001"
    runtime.close()


async def test_an_internally_emitted_event_has_no_receipt(tmp_path):
    """The field distinguishes "the world told us" from "we concluded"."""
    from nexus_seed.processes.semantic import bootstrap_semantic

    runtime = Runtime(tmp_path / "trace.db")
    bootstrap_semantic(runtime)

    await ingress(runtime).ingest(
        manual_envelope(
            event_type="process_parameter_changed",
            payload={"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        )
    )

    external = [e for e in runtime.event_store.all() if e.is_external]
    internal = [e for e in runtime.event_store.all() if not e.is_external]
    assert [e.type for e in external] == ["process_parameter_changed"]
    assert {e.type for e in internal} == {"state_delta_created", "state_changed"}
    assert all(runtime.get_ingress_receipt_for_event(e.id) is None for e in internal)
    runtime.close()


async def test_the_trace_reaches_from_the_receipt_to_the_action(tmp_path):
    """AT15 + spec §42: ingress and action traces join into one chain."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "trace.db")
    full_stack(runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))])

    result = await ingress(runtime).ingest(manual_envelope(source_event_key="msg-1"))

    trace = runtime.get_ingress_trace(result.event.id)
    assert trace is not None
    assert trace.source_identity == ("manual", "msg-1")
    assert trace.event.type == "human_message"
    assert [o.predicate for o in trace.observations] == ["analysis_completed"]
    assert [(d.entity, d.attribute) for d in trace.state_deltas] == [
        ("D1_CD", "analysis_result")
    ]
    assert [w.work_type for w in trace.work_requirements] == ["write_analysis_result"]
    assert [p.action_type for p in trace.action_proposals] == ["write_file"]
    assert trace.reached_the_world

    # And the outbound half still resolves on its own, back to the same message.
    action_trace = runtime.get_action_trace(trace.action_proposals[0].id)
    assert action_trace.source_event.id == result.event.id
    assert action_trace.source_event.ingress_receipt_id == trace.receipt.id
    runtime.close()


async def test_the_trace_can_start_from_the_external_identity_alone(tmp_path):
    """An operator has the provider's delivery id, not a NEXUS uuid."""
    root = tmp_path / "sandbox"
    runtime = Runtime(tmp_path / "trace.db")
    full_stack(runtime, root, llm_script=[proposal_response(analysis_proposal(0.95))])
    await ingress(runtime).ingest(manual_envelope(source_event_key="delivery-abc"))

    trace = runtime.get_ingress_trace_by_source_key("manual", "delivery-abc")

    assert trace is not None
    assert trace.event is not None
    assert [w.work_type for w in trace.work_requirements] == ["write_analysis_result"]
    assert (root / "D1_CD_analysis.txt").exists()
    runtime.close()


async def test_tracing_an_internal_event_returns_none(tmp_path):
    from nexus_seed.core.event import Event

    runtime = Runtime(tmp_path / "trace.db")
    event = Event("internal", "process", {})
    runtime.event_store.append(event)

    assert runtime.get_ingress_trace(event.id) is None
    assert runtime.get_ingress_trace_by_source_key("manual", "nope") is None
    runtime.close()


async def test_a_rejected_delivery_leaves_a_trace_with_no_event(tmp_path):
    runtime = Runtime(tmp_path / "trace.db")
    envelope = manual_envelope(source_event_key="bad-1")
    envelope.event_type = ""
    await ingress(runtime).ingest(envelope)

    trace = runtime.get_ingress_trace_by_source_key("manual", "bad-1")
    assert trace is not None
    assert trace.event is None
    assert trace.observations == []
    assert not trace.reached_the_world
    runtime.close()
