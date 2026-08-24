"""A folder's standing situation as an authorized observation source.

The file observer answers "this file changed". This answers a different
question: how much is sitting in there, how old the oldest thing is, what
kinds of file they are — a backlog is a fact about the world even on a day
when nothing changed, and only a standing reading can notice one.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from nexus_seed.cockpit import CockpitService
from nexus_seed.knowledge.autonomous_loop import KIND_SOURCE_OBSERVATION, KnowledgeLoop
from nexus_seed.knowledge_cli import main
from nexus_seed.observation_sources import (
    FOLDER_STATUS,
    FOLDER_STATUS_EVENT,
    FolderStatusAdapter,
    ObservationSource,
    ObservationSourceService,
)
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.runtime.runtime import Runtime


def make_folder(tmp_path, names, *, subdir_names=()):
    folder = tmp_path / "inbox"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / name).write_text("x" * 10, encoding="utf-8")
    if subdir_names:
        nested = folder / "sub"
        nested.mkdir(exist_ok=True)
        for name in subdir_names:
            (nested / name).write_text("y" * 5, encoding="utf-8")
    return folder


def source_for(folder, fields, *, recursive=True):
    return ObservationSource(
        name="inbox",
        kind=FOLDER_STATUS,
        fields=tuple(fields),
        config={"path": str(folder), "recursive": recursive},
    )


def build(tmp_path):
    runtime = Runtime(tmp_path / "loop.db")
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator, backend=None)
    runtime.knowledge_loop = loop
    service = ObservationSourceService(runtime, data_root=tmp_path)
    runtime.observation_sources = service
    return runtime, loop, service


# --- the adapter -------------------------------------------------------------


def test_reads_only_the_fields_that_were_authorized(tmp_path):
    folder = make_folder(tmp_path, ["a.txt", "b.txt"])
    adapter = FolderStatusAdapter(source_for(folder, ["file_count"]))

    values = adapter.snapshot()

    assert values == {"file_count": 2}  # nothing else was asked for


def test_reports_size_age_and_kinds(tmp_path):
    folder = make_folder(tmp_path, ["a.txt", "b.pptx"])
    adapter = FolderStatusAdapter(
        source_for(folder, ["file_count", "total_bytes", "oldest_change",
                            "newest_change", "by_extension"])
    )

    values = adapter.snapshot()

    assert values["file_count"] == 2
    assert values["total_bytes"] == 20
    assert values["by_extension"] == {".pptx": 1, ".txt": 1}
    assert values["oldest_change"] <= values["newest_change"]


def test_counts_subfolders_unless_told_not_to(tmp_path):
    folder = make_folder(tmp_path, ["a.txt"], subdir_names=["deep.txt"])

    recursive = FolderStatusAdapter(source_for(folder, ["file_count"])).snapshot()
    flat = FolderStatusAdapter(
        source_for(folder, ["file_count"], recursive=False)
    ).snapshot()

    assert recursive["file_count"] == 2
    assert flat["file_count"] == 1


def test_never_opens_a_file(tmp_path, monkeypatch):
    """Authorizing a folder here does not authorize reading what is in it."""
    folder = make_folder(tmp_path, ["secret.txt"])
    opened = []
    real_open = open

    def watched_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", watched_open)
    FolderStatusAdapter(
        source_for(folder, ["file_count", "total_bytes", "by_extension"])
    ).snapshot()

    assert [p for p in opened if "secret.txt" in p] == []


def test_an_empty_folder_reads_cleanly(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    values = FolderStatusAdapter(
        source_for(folder, ["file_count", "total_bytes", "oldest_change"])
    ).snapshot()
    assert values == {"file_count": 0, "total_bytes": 0, "oldest_change": None}


def test_a_missing_folder_is_a_clear_error(tmp_path):
    adapter = FolderStatusAdapter(source_for(tmp_path / "nope", ["file_count"]))
    with pytest.raises(ValueError, match="not a readable folder"):
        adapter.snapshot()


def test_a_folder_source_needs_a_path():
    with pytest.raises(ValueError, match=r"config\['path'\]"):
        FolderStatusAdapter(ObservationSource(name="x", kind=FOLDER_STATUS, fields=("file_count",)))


def test_an_unsupported_field_is_refused(tmp_path):
    folder = make_folder(tmp_path, ["a.txt"])
    with pytest.raises(ValueError, match="unsupported folder fields"):
        FolderStatusAdapter(source_for(folder, ["file_contents"]))


# --- the service, and reaching the World View --------------------------------


async def test_registering_refuses_a_folder_that_is_not_there(tmp_path):
    runtime, _loop, service = build(tmp_path)
    with pytest.raises(ValueError, match="not a readable folder"):
        service.create_folder_status(name="inbox", path=tmp_path / "missing")
    runtime.close()


async def test_a_folder_reading_becomes_a_world_fact(tmp_path):
    folder = make_folder(tmp_path, ["a.txt", "b.txt"])
    runtime, loop, service = build(tmp_path)

    source = service.create_folder_status(
        name="inbox", path=folder, fields=["file_count", "total_bytes"]
    )
    await service.poll_due(force_source_id=source.id)
    await loop.reconcile()

    entity = f"folder:{source.id}"
    view = loop.world_view()
    assert view[entity]["file_count"] == 2
    assert view[entity]["total_bytes"] == 20

    # The fact carries where it came from, like any other observation.
    [observation] = [
        item
        for item in loop.ledger.by_kind(KIND_SOURCE_OBSERVATION)
        if item.metadata.get("field") == "file_count"
    ]
    assert observation.source.type == "folder_status"
    assert observation.metadata["path"] == str(folder)
    runtime.close()


async def test_an_unchanged_folder_produces_no_second_event(tmp_path):
    folder = make_folder(tmp_path, ["a.txt"])
    runtime, loop, service = build(tmp_path)
    source = service.create_folder_status(
        name="inbox", path=folder, fields=["file_count"]
    )

    await service.poll_due(force_source_id=source.id)
    second = await service.poll_due(force_source_id=source.id)

    assert second[0]["status"] == "UNCHANGED"
    assert len(runtime.event_store.by_type(FOLDER_STATUS_EVENT)) == 1
    runtime.close()


async def test_a_growing_backlog_is_noticed(tmp_path):
    """The point of the source: the situation changed, no file was edited."""
    folder = make_folder(tmp_path, ["a.txt"])
    runtime, loop, service = build(tmp_path)
    source = service.create_folder_status(
        name="inbox", path=folder, fields=["file_count"]
    )
    await service.poll_due(force_source_id=source.id)
    await loop.reconcile()
    entity = f"folder:{source.id}"
    assert loop.world_view()[entity]["file_count"] == 1

    (folder / "b.txt").write_text("more", encoding="utf-8")
    await service.poll_due(force_source_id=source.id)
    await loop.reconcile()

    assert loop.world_view()[entity]["file_count"] == 2
    assert len(runtime.event_store.by_type(FOLDER_STATUS_EVENT)) == 2
    runtime.close()


async def test_one_failing_source_does_not_stop_the_others(tmp_path):
    folder = make_folder(tmp_path, ["a.txt"])
    runtime, loop, service = build(tmp_path)
    good = service.create_folder_status(name="ok", path=folder, fields=["file_count"])
    broken = service.create_folder_status(name="bad", path=folder, fields=["file_count"])
    # Authorized when registered, gone by the time it is read.
    broken.config["path"] = str(tmp_path / "vanished")
    service.store.save(broken)

    outcomes = {item["source_id"]: item for item in await service.poll_due()}

    assert outcomes[broken.id]["status"] == "ERROR"
    assert outcomes[good.id]["status"] != "ERROR"
    assert service.store.get(broken.id).last_error
    runtime.close()


# --- Cockpit and CLI ----------------------------------------------------------


async def test_cockpit_can_register_a_folder_source(tmp_path):
    folder = make_folder(tmp_path, ["a.txt"])
    runtime, loop, service = build(tmp_path)

    result = await CockpitService(runtime, master_id="operator").create_observation_source(
        name="inbox", fields=["file_count"], kind="folder_status", path=str(folder)
    )

    assert result["source"]["kind"] == "folder_status"
    assert result["source"]["config"]["path"] == str(folder)
    sources = CockpitService(runtime, master_id="operator").snapshot()["knowledge"][
        "observation_sources"
    ]
    assert [s["kind"] for s in sources] == ["folder_status"]
    runtime.close()


async def test_cockpit_refuses_an_unknown_kind(tmp_path):
    runtime, _loop, _service = build(tmp_path)
    with pytest.raises(ValueError, match="unsupported observation source kind"):
        await CockpitService(runtime, master_id="operator").create_observation_source(
            name="x", fields=["file_count"], kind="whatever"
        )
    runtime.close()


def test_cli_watch_folder_registers_and_takes_a_first_reading(tmp_path, capsys):
    folder = make_folder(tmp_path, ["a.txt", "b.txt"])
    code = main([
        "watch-folder", "--db", str(tmp_path / "k.db"), str(folder),
        "--name", "inbox", "--fields", "file_count", "--json",
    ])
    out = capsys.readouterr().out

    assert code == 0
    result = json.loads(out)
    assert result["source"]["kind"] == "folder_status"
    assert result["source"]["fields"] == ["file_count"]
    assert result["outcomes"][0]["status"] == "ACCEPTED"


def test_cli_watch_folder_reports_a_missing_folder_cleanly(tmp_path, capsys):
    code = main([
        "watch-folder", "--db", str(tmp_path / "k.db"), str(tmp_path / "nope"),
        "--name", "inbox",
    ])
    err = capsys.readouterr().err
    assert code == 1
    assert "not a readable folder" in err
    assert "Traceback" not in err


def test_the_cockpit_page_offers_a_folder_source_form():
    from nexus_seed.cockpit.assets import APP_JS

    assert "folderForm" in APP_JS
    assert "folder_status" in APP_JS
    assert "by_extension" in APP_JS
