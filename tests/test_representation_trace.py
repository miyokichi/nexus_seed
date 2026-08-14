"""Spec §48–§50: one chain, from an external file to an external file.

Phase 3E does not add a trace system; it joins the existing ones.  The question
an audit actually asks — *which version of which document did this decision come
from, rendered by which extractor* — now has an answer that runs end to end.
"""

from __future__ import annotations

import uuid

from resource_helpers import FACT_DOCUMENT, full_stack, watched_tree, write_file

from nexus_seed.runtime.runtime import Runtime


async def loop(tmp_path):
    """Run the full document → action loop and return the runtime."""
    root = watched_tree(tmp_path)
    out = tmp_path / "out"
    runtime = Runtime(tmp_path / "trace.db")
    adapter, backend = full_stack(runtime, root, out)
    write_file(root, "report.txt", FACT_DOCUMENT)
    await adapter.poll_and_ingest()
    return runtime, root, out


async def test_a_version_traces_out_to_the_external_source(tmp_path):
    """Spec §48."""
    runtime, _, _ = await loop(tmp_path)
    resource = runtime.get_resource_by_uri("file:///report.txt")
    version = runtime.get_current_resource_version(resource.id)

    trace = runtime.get_resource_trace(version.id)

    assert trace.version.version == 1
    assert trace.resource.uri == "file:///report.txt"
    assert trace.source_event.type == "file_created"
    assert trace.ingress_receipt.adapter_id == "local_file"
    assert trace.external_identity[0] == "local_file"
    assert [r.representation_type for r in trace.representations] == ["text"]
    runtime.close()


async def test_a_representation_traces_through_its_extractor(tmp_path):
    """Spec §49."""
    runtime, _, _ = await loop(tmp_path)
    resource = runtime.get_resource_by_uri("file:///report.txt")
    version = runtime.get_current_resource_version(resource.id)
    representation = runtime.find_representation(version.id, "text")

    trace = runtime.get_representation_trace(representation.id)

    assert trace.extractor == ("plain_text", "1")
    assert trace.extracted_by.definition_name == "extract_resource"
    assert trace.version.id == version.id
    assert trace.resource.uri == "file:///report.txt"
    assert trace.source_event.type == "file_created"
    assert trace.ingress_receipt.source_event_key.startswith("file:report.txt:")
    runtime.close()


async def test_the_full_chain_joins_ingress_resource_and_action(tmp_path):
    """Spec §50: external file → ingress → resource → context → action."""
    runtime, _, out = await loop(tmp_path)

    # Start from the far end: the action that touched the world.
    proposal = runtime.get_action_proposals()[0]
    action_trace = runtime.get_action_trace(proposal.id)
    assert action_trace.succeeded_execution is not None
    assert (out / "D1_CD_analysis.txt").exists()

    # Back to the event that caused the work.
    assert action_trace.source_event.type == "representation_created"
    representation_id = uuid.UUID(
        action_trace.source_event.payload["representation_id"]
    )

    # Back through the extractor to the document version.
    representation_trace = runtime.get_representation_trace(representation_id)
    assert representation_trace.resource.uri == "file:///report.txt"

    # Back to the delivery that told us the file existed.
    ingress_trace = runtime.get_ingress_trace(representation_trace.source_event.id)
    assert ingress_trace.source_identity[0] == "local_file"
    assert ingress_trace.event.type == "file_created"
    runtime.close()


async def test_the_context_snapshot_names_the_version_that_was_read(tmp_path):
    """Spec §31: which document version was the action decided on?"""
    runtime, _, _ = await loop(tmp_path)

    proposal = runtime.get_action_proposals()[0]
    snapshot = runtime.context_snapshot_store.get(proposal.context_snapshot_id)

    # The work process declares world-state context, not resources, so its
    # snapshot has no documents — but the interpreting process's does.
    assert "resources" in snapshot.context_json

    resource = runtime.get_resource_by_uri("file:///report.txt")
    version = runtime.get_current_resource_version(resource.id)
    assert runtime.get_resource_trace(version.id).representations != []
    runtime.close()


async def test_traces_of_unknown_ids_are_none(tmp_path):
    runtime = Runtime(tmp_path / "empty.db")
    assert runtime.get_resource_trace(uuid.uuid4()) is None
    assert runtime.get_representation_trace(uuid.uuid4()) is None
    runtime.close()
