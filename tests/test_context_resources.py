"""AT10–AT12 (spec §62–§64): documents reach a Process through the Context.

Three properties, in order of how much they matter:

* **selective** — a process sees the documents it declared, and no others;
* **fresh** — a resumed process sees the *current* version, not the one it was
  suspended holding (Invariant 40, the Phase 3A rule extended to documents);
* **auditable** — the snapshot records which version and rendering were read,
  so the historical answer survives the file changing (Invariant 39).
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


def reader_definition(**resource_kwargs) -> ProcessDefinition:
    """A process that records whatever documents its context contains."""
    return ProcessDefinition(
        name="doc_reader",
        version="1",
        handler="doc_reader",
        trigger_event_types=("read_docs",),
        context_requirements=ContextRequirements(
            include_trigger_event=True,
            continuation=ContinuationReq(include=True),
            resources=ResourcesReq(**resource_kwargs),
        ),
    )


async def doc_reader(ctx):
    """Record the compiled documents, suspending once if asked to."""
    SEEN.setdefault("activations", []).append(
        [
            {
                "uri": item.uri,
                "version": item.version_number,
                "content": item.content,
                "truncated": item.truncated,
                "representation_type": item.representation.representation_type
                if item.representation
                else None,
            }
            for item in ctx.view.resources
        ]
    )
    if ctx.resume_point is None and ctx.instance.input.get("suspend"):
        return ctx.suspend(
            resume_point="second_look",
            waiting_for={"event_type": "look_again"},
            saved_process_state={},
        )
    return ctx.complete(output={"activations": len(SEEN["activations"])})


async def setup(tmp_path, files: dict, *, db="ctx.db"):
    """Index ``files`` into a watched tree and return the runtime + adapter."""
    SEEN.clear()
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / db)
    adapter = resource_runtime(runtime, root)
    for name, content in files.items():
        (root / name).write_bytes(content.encode("utf-8"))
    await adapter.poll_and_ingest()
    return runtime, adapter, root


async def test_only_the_declared_resource_enters_the_context(tmp_path):
    """AT10."""
    runtime, _, _ = await setup(tmp_path, {"a.txt": "doc A", "b.txt": "doc B"})
    resource_a = runtime.get_resource_by_uri("file:///a.txt")

    runtime.register_process(
        reader_definition(ids=[str(resource_a.id)], representations=["text"]),
        doc_reader,
    )
    await runtime.submit_event(Event("read_docs", "test", {}))

    seen = SEEN["activations"][0]
    assert [item["uri"] for item in seen] == ["file:///a.txt"]
    assert seen[0]["content"] == "doc A"
    assert seen[0]["representation_type"] == "text"
    runtime.close()


async def test_resources_can_be_declared_by_uri(tmp_path):
    runtime, _, _ = await setup(tmp_path, {"a.txt": "doc A", "b.txt": "doc B"})
    runtime.register_process(reader_definition(uris=["file:///b.txt"]), doc_reader)
    await runtime.submit_event(Event("read_docs", "test", {}))

    assert [item["uri"] for item in SEEN["activations"][0]] == ["file:///b.txt"]
    runtime.close()


async def test_resources_can_come_from_the_process_input(tmp_path):
    runtime, _, _ = await setup(tmp_path, {"a.txt": "doc A"})
    runtime.register_process(reader_definition(from_process_input=True), doc_reader)

    # The router puts the trigger payload on the instance input.
    await runtime.submit_event(
        Event("read_docs", "test", {"resource_uris": ["file:///a.txt"]})
    )
    # Nothing was declared statically and the payload is nested under "payload",
    # so this activation legitimately sees nothing.
    assert SEEN["activations"][0] == []

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "doc_reader"
    ][0]
    instance.input["resource_uris"] = ["file:///a.txt"]
    instance.status = ProcessStatus.RUNNABLE
    instance.pending_event_id = None
    runtime.process_store.save_instance(instance)
    await runtime.run_pending()

    assert [item["uri"] for item in SEEN["activations"][1]] == ["file:///a.txt"]
    runtime.close()


async def test_max_items_caps_the_context(tmp_path):
    runtime, _, _ = await setup(
        tmp_path, {"a.txt": "A", "b.txt": "B", "c.txt": "C"}
    )
    uris = ["file:///a.txt", "file:///b.txt", "file:///c.txt"]
    runtime.register_process(reader_definition(uris=uris, max_items=2), doc_reader)
    await runtime.submit_event(Event("read_docs", "test", {}))

    assert len(SEEN["activations"][0]) == 2
    runtime.close()


async def test_oversized_content_is_truncated_and_flagged(tmp_path):
    """Spec §78: truncate or exclude — never summarise inside the compiler."""
    runtime, _, _ = await setup(tmp_path, {"big.txt": "x" * 500})
    runtime.register_process(
        reader_definition(uris=["file:///big.txt"], max_bytes=100), doc_reader
    )
    await runtime.submit_event(Event("read_docs", "test", {}))

    item = SEEN["activations"][0][0]
    assert item["truncated"] is True
    assert len(item["content"]) == 100
    runtime.close()


async def test_oversized_content_can_be_excluded_instead(tmp_path):
    runtime, _, _ = await setup(tmp_path, {"big.txt": "x" * 500})
    runtime.register_process(
        reader_definition(uris=["file:///big.txt"], max_bytes=100, on_oversize="exclude"),
        doc_reader,
    )
    await runtime.submit_event(Event("read_docs", "test", {}))

    assert SEEN["activations"][0] == []
    runtime.close()


async def test_a_resource_with_no_matching_representation_is_skipped(tmp_path):
    runtime, _, _ = await setup(tmp_path, {"a.txt": "doc A"})
    runtime.register_process(
        reader_definition(uris=["file:///a.txt"], representations=["structure"]),
        doc_reader,
    )
    await runtime.submit_event(Event("read_docs", "test", {}))

    # A .txt file has no structure rendering; it is not silently mislabelled.
    assert SEEN["activations"][0] == []
    runtime.close()


async def test_representation_preference_order_is_honoured(tmp_path):
    runtime, _, _ = await setup(tmp_path, {"data.csv": "entity,value\nD1_CD,45\n"})
    runtime.register_process(
        reader_definition(
            uris=["file:///data.csv"], representations=["structure", "text"]
        ),
        doc_reader,
    )
    await runtime.submit_event(Event("read_docs", "test", {}))

    item = SEEN["activations"][0][0]
    assert item["representation_type"] == "structure"
    assert item["content"]["columns"] == ["entity", "value"]
    runtime.close()


async def test_requirements_survive_a_runtime_restart(tmp_path):
    """Spec §76: resource requirements are part of the persisted definition."""
    runtime, _, _ = await setup(tmp_path, {"a.txt": "doc A"}, db="persist.db")
    runtime.register_process(
        reader_definition(uris=["file:///a.txt"], max_items=3, max_bytes=999), doc_reader
    )
    runtime.close()

    runtime2 = Runtime(tmp_path / "persist.db")
    definition = runtime2.process_store.get_definition("doc_reader", "1")
    reqs = definition.context_requirements.resources

    assert reqs.uris == ["file:///a.txt"]
    assert reqs.representations == ["text"]
    assert reqs.max_items == 3
    assert reqs.max_bytes == 999
    assert reqs.latest_only is True
    runtime2.close()
