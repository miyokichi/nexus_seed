"""AT12 (spec §31, §64): the snapshot answers "what was it looking at then?".

Two forces pulling opposite ways, both necessary:

* the **Context** is always fresh — a resumed process reads the current version;
* the **ContextSnapshot** is always historical — it keeps the version and the
  rendering that each activation actually read.

Without the first, long-running work reasons from stale documents.  Without the
second, an audit after the file changed can never reconstruct the basis of a
decision (Invariant 39).
"""

from __future__ import annotations

import uuid

from resource_helpers import resource_runtime, watched_tree, write_file

from nexus_seed.context.requirements import (
    ContextRequirements,
    ContinuationReq,
    ResourcesReq,
)
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.runtime.runtime import Runtime

READER = ProcessDefinition(
    name="doc_auditor",
    version="1",
    handler="doc_auditor",
    trigger_event_types=("audit",),
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        continuation=ContinuationReq(include=True),
        resources=ResourcesReq(uris=["file:///spec.txt"], representations=["text"]),
    ),
)


async def doc_auditor(ctx):
    item = ctx.view.get_resource("file:///spec.txt")
    return ctx.complete(output={"read": item.content if item else None})


async def setup(tmp_path, content="target=48"):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "snap.db")
    adapter = resource_runtime(runtime, root)
    runtime.register_process(READER, doc_auditor)
    write_file(root, "spec.txt", content)
    await adapter.poll_and_ingest()
    return runtime, adapter, root


def snapshot_resources(runtime, definition_name="doc_auditor"):
    instances = [
        i for i in runtime.process_store.all_instances() if i.definition_name == definition_name
    ]
    return [
        runtime.get_latest_context_snapshot(i.id).context_json["resources"]
        for i in instances
    ]


async def test_the_snapshot_records_all_three_ids(tmp_path):
    """Spec §31: resource, version and representation, never the content."""
    runtime, _, _ = await setup(tmp_path)
    await runtime.submit_event(Event("audit", "test", {}))

    recorded = snapshot_resources(runtime)[0][0]
    resource = runtime.get_resource_by_uri("file:///spec.txt")
    version = runtime.get_current_resource_version(resource.id)

    assert recorded["resource_id"] == str(resource.id)
    assert recorded["resource_version_id"] == str(version.id)
    assert recorded["version"] == 1
    assert recorded["uri"] == "file:///spec.txt"
    assert recorded["content_hash"] == version.content_hash
    assert recorded["representation_type"] == "text"
    # The bytes stay where they already are, under their representation id.
    assert "content" not in recorded
    runtime.close()


async def test_two_activations_snapshot_two_different_versions(tmp_path):
    """AT12: each run's snapshot pins what that run read."""
    runtime, adapter, root = await setup(tmp_path, "target=48")
    await runtime.submit_event(Event("audit", "test", {}))

    write_file(root, "spec.txt", "target=45")
    await adapter.poll_and_ingest()
    await runtime.submit_event(Event("audit", "test", {}))

    first, second = [entries[0] for entries in snapshot_resources(runtime)]

    assert first["version"] == 1 and second["version"] == 2
    assert first["resource_version_id"] != second["resource_version_id"]
    assert first["representation_id"] != second["representation_id"]
    assert first["content_hash"] != second["content_hash"]
    runtime.close()


async def test_the_recorded_rendering_is_still_readable_afterwards(tmp_path):
    """A snapshot id is only useful if it still resolves once the file moved on."""
    runtime, adapter, root = await setup(tmp_path, "target=48")
    await runtime.submit_event(Event("audit", "test", {}))

    write_file(root, "spec.txt", "target=45")
    await adapter.poll_and_ingest()

    recorded = snapshot_resources(runtime)[0][0]
    representation = runtime.get_representation(uuid.UUID(recorded["representation_id"]))

    assert representation.content == "target=48"
    # Meanwhile the world has moved on.
    resource = runtime.get_resource_by_uri("file:///spec.txt")
    assert runtime.get_current_resource_version(resource.id).version == 2
    runtime.close()


async def test_truncation_is_recorded_so_an_audit_is_not_misled(tmp_path):
    """A process that saw only part of a document must be seen to have done so."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "trunc.db")
    adapter = resource_runtime(runtime, root)
    runtime.register_process(
        ProcessDefinition(
            name="doc_auditor",
            version="1",
            handler="doc_auditor",
            trigger_event_types=("audit",),
            context_requirements=ContextRequirements(
                include_trigger_event=True,
                resources=ResourcesReq(
                    uris=["file:///spec.txt"], representations=["text"], max_bytes=10
                ),
            ),
        ),
        doc_auditor,
    )
    write_file(root, "spec.txt", "x" * 200)
    await adapter.poll_and_ingest()
    await runtime.submit_event(Event("audit", "test", {}))

    recorded = snapshot_resources(runtime)[0][0]
    assert recorded["truncated"] is True
    # And the full rendering is still on record, so the audit can see the rest.
    assert len(runtime.get_representation(uuid.UUID(recorded["representation_id"])).content) == 200
    runtime.close()


async def test_a_process_with_no_resource_requirements_records_none(tmp_path):
    runtime, _, _ = await setup(tmp_path)
    runtime.register_process(
        ProcessDefinition(
            name="plain",
            version="1",
            handler="plain",
            trigger_event_types=("plain_audit",),
        ),
        doc_auditor,
    )
    await runtime.submit_event(Event("plain_audit", "test", {}))

    assert snapshot_resources(runtime, "plain") == [[]]
    runtime.close()
