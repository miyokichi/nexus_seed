"""AT19 (spec §71): a document read from Context drives an action.

The narrow claim: a Process that decides what to do *based on a document* still
reaches the world only through the Phase 3C proposal boundary.  Documents get
no shortcut to acting, exactly as they get none to World State.
"""

from __future__ import annotations

from resource_helpers import (
    instances_named,
    resource_runtime,
    watched_tree,
    write_file,
)

from nexus_seed.backends import LocalFileActionBackend
from nexus_seed.context.requirements import (
    ContextRequirements,
    ContinuationReq,
    ResourcesReq,
)
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.processes.actions import (
    action_proposed_event,
    bootstrap_actions,
    waiting_for_action,
)
from nexus_seed.runtime.runtime import Runtime

REPORTER = ProcessDefinition(
    name="report_writer",
    version="1",
    handler="report_writer",
    trigger_event_types=("write_report",),
    metadata={"role": "work", "permissions": ["filesystem.write"]},
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        continuation=ContinuationReq(include=True),
        resources=ResourcesReq(uris=["file:///source.txt"], representations=["text"]),
    ),
)


async def report_writer(ctx):
    """Turn the source document into a proposal to write a derived one."""
    if ctx.resume_point == "await_action":
        return ctx.complete(
            output={
                "outcome": ctx.event.type,
                "action_proposal_id": ctx.event.payload.get("action_proposal_id"),
            }
        )

    item = ctx.view.get_resource("file:///source.txt")
    if item is None:
        return ctx.fail("source document is not in context")

    proposal = ctx.propose_action(
        backend="local_file",
        action_type="write_file",
        target="derived.txt",
        parameters={
            "content": f"source={item.uri}\nversion={item.version_number}\n"
            f"body={item.content}"
        },
        required_permissions=["filesystem.write"],
        declared_side_effects=["filesystem_write"],
        risk_level="LOW",
        rationale="write a derived report from the source document",
    )
    return ctx.suspend(
        resume_point="await_action",
        waiting_for=waiting_for_action(proposal),
        saved_process_state={"action_proposal_id": str(proposal.id)},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


async def setup(tmp_path, content="D1_CD.analysis_result=within spec\n"):
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "act.db")
    adapter = resource_runtime(runtime, root)
    bootstrap_actions(runtime)
    backend = LocalFileActionBackend(out)
    runtime.register_backend("local_file", backend)
    runtime.register_process(REPORTER, report_writer)

    write_file(root, "source.txt", content)
    await adapter.poll_and_ingest()
    return runtime, adapter, root, out, backend


async def test_a_document_in_context_drives_a_real_file_write(tmp_path):
    runtime, _, _, out, backend = await setup(tmp_path)

    await runtime.submit_event(Event("write_report", "test", {}))

    writer = instances_named(runtime, "report_writer")[0]
    assert writer.status is ProcessStatus.COMPLETED
    assert writer.local_state["output"]["outcome"] == "action_succeeded"

    proposal = runtime.get_action_proposals()[0]
    assert proposal.status.value == "SUCCEEDED"

    written = (out / "derived.txt").read_text(encoding="utf-8")
    assert "source=file:///source.txt" in written
    assert "version=1" in written
    assert "body=D1_CD.analysis_result=within spec" in written
    assert len(backend.calls) == 1
    runtime.close()


async def test_the_action_reflects_the_current_document_version(tmp_path):
    """A revised source produces a proposal built from the new text."""
    runtime, adapter, root, out, backend = await setup(tmp_path, "first version\n")

    write_file(root, "source.txt", "second version\n")
    await adapter.poll_and_ingest()

    await runtime.submit_event(Event("write_report", "test", {}))

    written = (out / "derived.txt").read_text(encoding="utf-8")
    assert "version=2" in written
    assert "body=second version" in written
    runtime.close()


async def test_the_proposal_records_the_context_it_was_decided_on(tmp_path):
    """Phase 3C's snapshot audit now names the document version too."""
    runtime, _, _, _, _ = await setup(tmp_path)
    await runtime.submit_event(Event("write_report", "test", {}))

    proposal = runtime.get_action_proposals()[0]
    snapshot = runtime.context_snapshot_store.get(proposal.context_snapshot_id)
    recorded = snapshot.context_json["resources"][0]

    assert recorded["uri"] == "file:///source.txt"
    assert recorded["version"] == 1
    assert recorded["representation_id"] is not None
    assert recorded["content_hash"].startswith("sha256-")
    runtime.close()


async def test_a_missing_document_stops_the_process_before_it_acts(tmp_path):
    """No document, no proposal — the action boundary is never reached."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "act.db")
    resource_runtime(runtime, root)
    bootstrap_actions(runtime)
    backend = LocalFileActionBackend(out)
    runtime.register_backend("local_file", backend)
    runtime.register_process(REPORTER, report_writer)

    await runtime.submit_event(Event("write_report", "test", {}))

    writer = instances_named(runtime, "report_writer")[0]
    assert writer.status is ProcessStatus.FAILED
    assert runtime.get_action_proposals() == []
    assert backend.calls == []
    runtime.close()
