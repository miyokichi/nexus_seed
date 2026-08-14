"""AT6–AT9 (spec §58–§61): renderings of a version, and where they came from.

A Representation belongs to the *Version*, and its identity includes the
extractor's name and version — so improving an extractor produces a new
rendering next to the old one instead of silently rewriting what a past
decision was based on.
"""

from __future__ import annotations

from resource_helpers import only_resource, resource_runtime, watched_tree

from nexus_seed.resources.extractors import (
    CSVExtractor,
    ExtractionError,
    JSONExtractor,
    PlainTextExtractor,
    default_registry,
)
from nexus_seed.runtime.runtime import Runtime


async def index(tmp_path, name, content, *, db="r.db"):
    """Index one file and return (runtime, resource, current version)."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / db)
    adapter = resource_runtime(runtime, root)
    # Bytes, not text: no platform newline translation, so content hashes and
    # extracted text are exactly what the test wrote.
    (root / name).write_bytes(content.encode("utf-8"))
    await adapter.poll_and_ingest()
    resource = only_resource(runtime)
    return runtime, resource, runtime.get_current_resource_version(resource.id)


# --- extractors in isolation ----------------------------------------------


def test_plain_text_extractor_decodes_utf8():
    assert PlainTextExtractor().extract("D1のCD".encode("utf-8")) == "D1のCD"


def test_plain_text_extractor_rejects_non_utf8():
    import pytest

    with pytest.raises(ExtractionError):
        PlainTextExtractor().extract(b"\xff\xfe\x00bad")


def test_json_extractor_parses_and_rejects():
    import pytest

    assert JSONExtractor().extract(b'{"a": [1, 2]}') == {"a": [1, 2]}
    with pytest.raises(ExtractionError):
        JSONExtractor().extract(b"{not json")


def test_csv_extractor_builds_columns_and_rows():
    result = CSVExtractor().extract(b"entity,value\nD1_CD,45\nD2_CD,50\n")
    assert result["columns"] == ["entity", "value"]
    assert result["rows"] == [
        {"entity": "D1_CD", "value": "45"},
        {"entity": "D2_CD", "value": "50"},
    ]
    assert result["row_count"] == 2


def test_csv_extractor_handles_an_empty_file():
    assert CSVExtractor().extract(b"") == {"columns": [], "rows": []}


def test_the_registry_is_a_deterministic_lookup():
    registry = default_registry()
    assert registry.find("text", "text").name == "plain_text"
    assert registry.find("structure", "csv").name == "csv"
    assert registry.find("structure", "json").name == "json"
    # No structure extractor claims plain text, and nothing claims PDFs yet.
    assert registry.find("structure", "text") is None
    assert registry.find("text", "pdf") is None


def test_a_later_registration_takes_precedence():
    registry = default_registry()
    replacement = PlainTextExtractor(version="2")
    registry.register(replacement)
    assert registry.find("text", "text").version == "2"


# --- extraction as a Process ----------------------------------------------


async def test_a_text_version_gets_a_text_representation(tmp_path):
    """AT6."""
    runtime, resource, version = await index(tmp_path, "report.txt", "hello world")

    representations = runtime.get_representations(version.id)
    assert [r.representation_type for r in representations] == ["text"]
    assert representations[0].content == "hello world"
    assert representations[0].extractor_name == "plain_text"
    assert representations[0].extractor_version == "1"
    assert representations[0].created_by_process_id is not None
    runtime.close()


async def test_the_representation_is_attached_to_the_version_not_the_resource(tmp_path):
    """Each version keeps its own rendering, so history stays readable."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)
    target = root / "report.txt"

    target.write_text("first", encoding="utf-8")
    await adapter.poll_and_ingest()
    target.write_text("second", encoding="utf-8")
    await adapter.poll_and_ingest()

    resource = only_resource(runtime)
    v1, v2 = runtime.get_resource_versions(resource.id)
    assert runtime.find_representation(v1.id, "text").content == "first"
    assert runtime.find_representation(v2.id, "text").content == "second"
    runtime.close()


async def test_structure_and_text_coexist_for_one_version(tmp_path):
    """AT8: a CSV legitimately has both renderings, kept apart."""
    runtime, resource, version = await index(
        tmp_path, "data.csv", "entity,value\nD1_CD,45\n"
    )

    types = sorted(r.representation_type for r in runtime.get_representations(version.id))
    assert types == ["structure", "text"]

    text = runtime.find_representation(version.id, "text")
    structure = runtime.find_representation(version.id, "structure")
    assert text.content == "entity,value\nD1_CD,45\n"
    assert text.extractor_name == "plain_text"
    assert structure.content["columns"] == ["entity", "value"]
    assert structure.content["rows"] == [{"entity": "D1_CD", "value": "45"}]
    assert structure.extractor_name == "csv"
    runtime.close()


async def test_json_gets_a_parsed_structure(tmp_path):
    runtime, resource, version = await index(
        tmp_path, "conf.json", '{"targets": {"D1_CD": 45}}'
    )
    structure = runtime.find_representation(version.id, "structure")
    assert structure.content == {"targets": {"D1_CD": 45}}
    assert structure.extractor_name == "json"
    runtime.close()


async def test_representation_traces_back_to_the_world(tmp_path):
    """AT9."""
    runtime, resource, version = await index(tmp_path, "report.txt", "hello")

    representation = runtime.get_representations(version.id)[0]
    trace = runtime.get_representation_trace(representation.id)

    assert trace.extractor == ("plain_text", "1")
    assert trace.version.id == version.id
    assert trace.resource.uri == "file:///report.txt"
    assert trace.extracted_by.definition_name == "extract_resource"
    assert trace.source_event.type == "file_created"
    assert trace.ingress_receipt.adapter_id == "local_file"
    runtime.close()


async def test_an_unreadable_version_reports_a_failure_event(tmp_path):
    """Spec §47: extraction failure is observable, not silent."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "r.db")
    adapter = resource_runtime(runtime, root)
    target = root / "report.txt"
    target.write_text("gone soon", encoding="utf-8")

    envelopes = await adapter.poll()
    target.unlink()  # vanishes between observation and extraction
    for envelope in envelopes:
        await runtime.ingress.ingest(envelope, checkpoint=adapter.checkpoint_for(envelope))

    assert len(runtime.event_store.by_type("representation_failed")) == 1
    resource = only_resource(runtime)
    version = runtime.get_current_resource_version(resource.id)
    assert runtime.get_representations(version.id) == []
    runtime.close()
