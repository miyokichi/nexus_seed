"""AT14 (spec §66, §37): a document reaches World State the ordinary way.

The rule this defends is the same one Phase 3B established for the LLM and 3C
for actions: no component gets a private path to the world model.  A document's
content becomes an Observation and a StateDelta, and flows through the existing
``apply_state_delta`` — so conflict checking, provenance and history all apply
to it exactly as they do to a sensor reading.
"""

from __future__ import annotations

from resource_helpers import (
    instances_named,
    resource_runtime,
    watched_tree,
    write_file,
)

from nexus_seed.processes.resources import parse_facts
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.runtime.runtime import Runtime


async def reading(tmp_path, content, *, db="interp.db"):
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / db)
    adapter = resource_runtime(runtime, root)
    bootstrap_semantic(runtime)
    write_file(root, "facts.txt", content)
    await adapter.poll_and_ingest()
    return runtime, adapter, root


# --- the parser ------------------------------------------------------------


def test_typed_values_are_recognised():
    facts = parse_facts("a.i=42\na.f=1.5\na.b=true\na.s=hello\n")
    assert facts == [
        ("a", "i", 42),
        ("a", "f", 1.5),
        ("a", "b", True),
        ("a", "s", "hello"),
    ]


def test_comments_blanks_and_malformed_lines_are_skipped():
    assert parse_facts("# note\n\nno-separator\n.missing=1\nx.=2\n") == []


def test_non_text_content_yields_nothing():
    assert parse_facts({"columns": []}) == []
    assert parse_facts(None) == []


# --- document to world state ----------------------------------------------


async def test_a_document_becomes_observation_delta_and_state(tmp_path):
    """AT14."""
    runtime, _, _ = await reading(tmp_path, "D1_CD.target=45\n")

    assert runtime.state_store.get("D1_CD", "target") == 45

    observation = runtime.observation_store.all()[0]
    assert observation.predicate == "facts_extracted"
    assert observation.extracted == {"D1_CD.target": 45}

    delta = runtime.state_delta_store.all()[0]
    assert (delta.entity, delta.attribute) == ("D1_CD", "target")
    assert delta.old_value is None and delta.new_value == 45
    assert delta.observation_id == observation.id
    assert "file:///facts.txt" in delta.reason
    runtime.close()


async def test_several_facts_in_one_document_all_land(tmp_path):
    runtime, _, _ = await reading(
        tmp_path, "D1_CD.target=45\nD1_CD.analysis_result=within spec\n"
    )

    assert runtime.state_store.get("D1_CD", "target") == 45
    assert runtime.state_store.get("D1_CD", "analysis_result") == "within spec"
    # One reading of the document, two conclusions from it.
    assert len(runtime.observation_store.all()) == 1
    assert len(runtime.state_delta_store.all()) == 2
    runtime.close()


async def test_state_provenance_reaches_back_to_the_document(tmp_path):
    runtime, _, _ = await reading(tmp_path, "D1_CD.target=45\n")

    provenance = runtime.get_state_provenance("D1_CD", "target")
    assert provenance.source_event.type == "representation_created"

    representation_id = provenance.source_event.payload["representation_id"]
    import uuid

    trace = runtime.get_representation_trace(uuid.UUID(representation_id))
    assert trace.resource.uri == "file:///facts.txt"
    assert trace.ingress_receipt.adapter_id == "local_file"
    runtime.close()


async def test_a_revised_document_produces_a_second_state_version(tmp_path):
    runtime, adapter, root = await reading(tmp_path, "D1_CD.target=48\n")
    write_file(root, "facts.txt", "D1_CD.target=45\n")
    await adapter.poll_and_ingest()

    history = runtime.get_state_history("D1_CD", "target")
    assert [h.value for h in history] == [48, 45]
    assert runtime.state_store.get("D1_CD", "target") == 45
    runtime.close()


async def test_a_document_that_restates_current_state_changes_nothing(tmp_path):
    """Reading the same fact again is not a state change."""
    runtime, adapter, root = await reading(tmp_path, "D1_CD.target=45\n")
    write_file(root, "copy.txt", "D1_CD.target=45\n")
    await adapter.poll_and_ingest()

    assert len(runtime.get_state_history("D1_CD", "target")) == 1
    assert len(runtime.state_delta_store.all()) == 1
    # The second document was still read and catalogued — it just said nothing new.
    assert len(runtime.get_resources()) == 2
    assert len(runtime.observation_store.all()) == 2
    runtime.close()


async def test_a_document_with_no_facts_is_read_and_ignored(tmp_path):
    runtime, _, _ = await reading(tmp_path, "just some prose, no facts here\n")

    assert runtime.observation_store.all() == []
    assert runtime.state_delta_store.all() == []
    interpreter = instances_named(runtime, "interpret_resource")[0]
    assert interpreter.local_state["output"]["interpreted"] == 0
    # But the document itself is still catalogued and rendered.
    resource = runtime.get_resource_by_uri("file:///facts.txt")
    version = runtime.get_current_resource_version(resource.id)
    assert runtime.find_representation(version.id, "text") is not None
    runtime.close()


async def test_a_structure_representation_is_not_read_as_facts(tmp_path):
    """The interpreter is explicit about what it understands."""
    root = watched_tree(tmp_path)
    runtime = Runtime(tmp_path / "csv.db")
    adapter = resource_runtime(runtime, root)
    bootstrap_semantic(runtime)

    write_file(root, "data.csv", "entity,value\nD1_CD,45\n")
    await adapter.poll_and_ingest()

    # Both renderings exist; only the text one was offered to the interpreter,
    # and it contains no `entity.attribute=value` lines.
    resource = runtime.get_resource_by_uri("file:///data.csv")
    version = runtime.get_current_resource_version(resource.id)
    assert len(runtime.get_representations(version.id)) == 2
    assert runtime.state_delta_store.all() == []
    runtime.close()
