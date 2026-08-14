"""AT7 (spec §59): a rendering is produced once, and never rewritten.

Two rules doing different jobs:

* the same extractor re-run on the same version yields **one** row — so a
  replayed activation or a re-scan cannot fill the table with copies;
* a *new extractor version* yields a **new** row beside the old one — so
  improving an extractor never rewrites the text a past decision was based on.
"""

from __future__ import annotations

from resource_helpers import only_resource, resource_runtime, watched_tree, write_file

from nexus_seed.core.event import Event
from nexus_seed.resources.extractors import PlainTextExtractor
from nexus_seed.resources.models import ResourceRepresentation
from nexus_seed.runtime.runtime import Runtime


async def indexed(tmp_path, content="hello", *, db="dedup.db"):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / db, )
    adapter = resource_runtime(runtime, root)
    write_file(root, "report.txt", content)
    await adapter.poll_and_ingest()
    resource = only_resource(runtime)
    return runtime, adapter, root, resource, runtime.get_current_resource_version(resource.id)


def re_request_extraction(runtime):
    """Ask for extraction again, as a fresh event carrying the same payload.

    Since Phase 3F a delivered event is not re-routed (there is no replay), so
    re-running extraction means raising a *new* request — which is the honest
    way to test that the dedup lives in the representation store rather than in
    the routing layer.
    """
    original = runtime.event_store.by_type("resource_version_created")[0]
    return Event("resource_version_created", "test", dict(original.payload))


async def test_re_extracting_the_same_version_adds_nothing(tmp_path):
    """AT7: re-running the pipeline over one version keeps one representation."""
    runtime, adapter, root, resource, version = await indexed(tmp_path)
    assert len(runtime.get_representations(version.id)) == 1

    for _ in range(2):
        await runtime.submit_event(re_request_extraction(runtime))

    assert len(runtime.get_representations(version.id)) == 1
    runtime.close()


async def test_repeated_scans_do_not_multiply_representations(tmp_path):
    runtime, adapter, root, resource, version = await indexed(tmp_path)

    for _ in range(3):
        await adapter.poll_and_ingest()

    assert len(runtime.get_representations(version.id)) == 1
    assert len(runtime.resource_store.all_representations()) == 1
    runtime.close()


async def test_a_duplicate_insert_returns_the_original_row(tmp_path):
    """The caller gets a real id back, not a row that was silently dropped."""
    runtime, adapter, root, resource, version = await indexed(tmp_path)
    original = runtime.get_representations(version.id)[0]

    duplicate = runtime.resource_store.save_representation(
        ResourceRepresentation(
            resource_version_id=version.id,
            representation_type="text",
            content="hello",
            extractor_name="plain_text",
            extractor_version="1",
        )
    )

    assert duplicate.id == original.id
    assert runtime.get_representation(duplicate.id) is not None
    runtime.close()


async def test_a_better_extractor_adds_a_rendering_instead_of_replacing_one(tmp_path):
    """Spec §14: the extractor version is part of a representation's identity."""
    runtime, adapter, root, resource, version = await indexed(tmp_path, "hello")
    original = runtime.find_representation(version.id, "text")
    assert original.extractor_version == "1"

    # Ship an improved extractor and re-run extraction for this version.
    runtime.extractors.register(PlainTextExtractor(version="2"))
    await runtime.submit_event(re_request_extraction(runtime))

    representations = runtime.get_representations(version.id)
    assert sorted(r.extractor_version for r in representations) == ["1", "2"]
    # The original rendering is untouched and still retrievable.
    assert runtime.get_representation(original.id).content == "hello"
    runtime.close()


async def test_each_version_gets_its_own_representation(tmp_path):
    """Dedup is per version — a new version is genuinely new work."""
    runtime, adapter, root, resource, _ = await indexed(tmp_path, "v1")
    write_file(root, "report.txt", "v2")
    await adapter.poll_and_ingest()

    v1, v2 = runtime.get_resource_versions(resource.id)
    assert runtime.find_representation(v1.id, "text").content == "v1"
    assert runtime.find_representation(v2.id, "text").content == "v2"
    assert len(runtime.resource_store.all_representations()) == 2
    runtime.close()
