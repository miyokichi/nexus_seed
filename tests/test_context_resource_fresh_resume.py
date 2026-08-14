"""AT11 + AT12 (spec §63, §64): a resumed process reads the *current* document.

This is Phase 3A's fresh-resume rule extended to documents (Invariant 40), and
it is the property that makes long-running work trustworthy: a process that
suspended holding v1 of a spec, and resumes after the spec was revised, must not
keep reasoning from the old text.

The ContextSnapshot pulls the other way on purpose — it keeps pointing at the
version each activation actually read, so "what was it looking at *then*"
remains answerable (Invariant 39).
"""

from __future__ import annotations

from resource_helpers import resource_runtime, watched_tree

from nexus_seed.context.requirements import (
    ContextRequirements,
    ContinuationReq,
    ResourcesReq,
)
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.runtime.runtime import Runtime

SEEN: dict = {}

READER = ProcessDefinition(
    name="spec_reader",
    version="1",
    handler="spec_reader",
    trigger_event_types=("read_spec",),
    context_requirements=ContextRequirements(
        include_trigger_event=True,
        continuation=ContinuationReq(include=True),
        resources=ResourcesReq(uris=["file:///spec.txt"], representations=["text"]),
    ),
)


async def spec_reader(ctx):
    """Read the spec, suspend for approval, then read it again on resume."""
    item = ctx.view.get_resource("file:///spec.txt")
    SEEN.setdefault("reads", []).append(
        {"version": item.version_number, "content": item.content} if item else None
    )
    if ctx.resume_point is None:
        return ctx.suspend(
            resume_point="after_approval",
            waiting_for={"event_type": "spec_approved"},
            saved_process_state={},
        )
    return ctx.complete(output={"reads": len(SEEN["reads"])})


async def test_resume_sees_the_new_version(tmp_path):
    """AT11: the world moved on while the process was suspended."""
    SEEN.clear()
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "fresh.db")
    adapter = resource_runtime(runtime, root)
    spec = root / "spec.txt"

    spec.write_bytes(b"target=48")
    await adapter.poll_and_ingest()

    runtime.register_process(READER, spec_reader)
    await runtime.submit_event(Event("read_spec", "test", {}))

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "spec_reader"
    ][0]
    assert instance.status is ProcessStatus.SUSPENDED
    assert SEEN["reads"][0] == {"version": 1, "content": "target=48"}

    # The document is revised while the process waits.
    spec.write_bytes(b"target=45")
    await adapter.poll_and_ingest()
    resource = runtime.get_resource_by_uri("file:///spec.txt")
    assert len(runtime.get_resource_versions(resource.id)) == 2

    await runtime.submit_event(Event("spec_approved", "human", {}))

    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.COMPLETED
    # The resumed activation read v2, not the text it suspended holding.
    assert SEEN["reads"][1] == {"version": 2, "content": "target=45"}
    runtime.close()


async def test_snapshots_keep_the_version_each_activation_read(tmp_path):
    """AT12: current context is fresh; past snapshots stay historical."""
    SEEN.clear()
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "snap.db")
    adapter = resource_runtime(runtime, root)
    spec = root / "spec.txt"

    spec.write_bytes(b"target=48")
    await adapter.poll_and_ingest()
    runtime.register_process(READER, spec_reader)
    await runtime.submit_event(Event("read_spec", "test", {}))

    spec.write_bytes(b"target=45")
    await adapter.poll_and_ingest()
    await runtime.submit_event(Event("spec_approved", "human", {}))

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "spec_reader"
    ][0]
    snapshots = runtime.get_context_snapshots(instance.id)
    assert len(snapshots) == 2

    first = snapshots[0].context_json["resources"][0]
    second = snapshots[1].context_json["resources"][0]

    assert first["version"] == 1 and second["version"] == 2
    assert first["resource_version_id"] != second["resource_version_id"]
    assert first["representation_id"] != second["representation_id"]
    assert first["uri"] == second["uri"] == "file:///spec.txt"

    # Both renderings are still readable from their recorded ids.
    import uuid

    assert runtime.get_representation(uuid.UUID(first["representation_id"])).content == (
        "target=48"
    )
    assert runtime.get_representation(uuid.UUID(second["representation_id"])).content == (
        "target=45"
    )
    runtime.close()


async def test_a_process_can_pin_itself_to_a_version(tmp_path):
    """``latest_only=False`` keeps a process reading what it started with."""
    SEEN.clear()
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "pin.db")
    adapter = resource_runtime(runtime, root)
    spec = root / "spec.txt"

    spec.write_bytes(b"target=48")
    await adapter.poll_and_ingest()
    resource = runtime.get_resource_by_uri("file:///spec.txt")
    v1 = runtime.get_current_resource_version(resource.id)

    pinned = ProcessDefinition(
        name="spec_reader",
        version="1",
        handler="spec_reader",
        trigger_event_types=("read_spec",),
        context_requirements=ContextRequirements(
            include_trigger_event=True,
            continuation=ContinuationReq(include=True),
            resources=ResourcesReq(
                uris=["file:///spec.txt"], representations=["text"], latest_only=False
            ),
        ),
    )
    runtime.register_process(pinned, spec_reader)
    await runtime.submit_event(Event("read_spec", "test", {}))

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "spec_reader"
    ][0]
    instance.input["resource_versions"] = {str(resource.id): str(v1.id)}
    runtime.process_store.save_instance(instance)

    spec.write_bytes(b"target=45")
    await adapter.poll_and_ingest()
    await runtime.submit_event(Event("spec_approved", "human", {}))

    # Deliberately still reading v1 after the file changed.
    assert SEEN["reads"][1] == {"version": 1, "content": "target=48"}
    runtime.close()


async def test_resources_survive_a_runtime_restart(tmp_path):
    """AT5 (spec §57): catalogue and history rebuild from SQLite alone."""
    root = watched_tree(tmp_path)
    db_path = tmp_path / "restart.db"

    runtime = Runtime(db_path)
    adapter = resource_runtime(runtime, root)
    spec = root / "spec.txt"
    for content in (b"v1", b"v2", b"v3"):
        spec.write_bytes(content)
        await adapter.poll_and_ingest()

    resource_id = runtime.get_resource_by_uri("file:///spec.txt").id
    assert [v.version for v in runtime.get_resource_versions(resource_id)] == [1, 2, 3]
    runtime.close()

    runtime2 = Runtime(db_path)
    resource_runtime(runtime2, root)

    resource = runtime2.get_resource_by_uri("file:///spec.txt")
    assert resource.id == resource_id
    assert [v.version for v in runtime2.get_resource_versions(resource.id)] == [1, 2, 3]
    assert runtime2.get_current_resource_version(resource.id).version == 3
    assert runtime2.get_resource_version(resource.id, 1).content_hash is not None

    # Re-scanning the unchanged tree adds nothing.
    adapter2 = [a for a in runtime2.adapters][0]
    await adapter2.poll_and_ingest()
    assert len(runtime2.get_resource_versions(resource.id)) == 3
    runtime2.close()
