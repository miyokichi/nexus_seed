"""AT1/AT2 via the manual adapter and its CLI (spec §24, §25)."""

from __future__ import annotations

import json

from ingress_helpers import ingress

from nexus_seed.adapters.manual import ManualAdapter
from nexus_seed.ingress.models import IngressStatus
from nexus_seed.ingress_cli import build_parser, main, run
from nexus_seed.runtime.runtime import Runtime


def test_the_adapter_only_describes_what_it_was_given():
    adapter = ManualAdapter()
    envelope = adapter.envelope(
        event_type="human_message",
        source_event_key="demo-001",
        payload={"text": "hello"},
        metadata={"who": "alex"},
    )
    assert envelope.adapter_id == "manual"
    assert envelope.source_type == "manual"
    assert envelope.source_event_key == "demo-001"
    assert envelope.event_type == "human_message"
    assert envelope.payload == {"text": "hello"}
    assert envelope.metadata == {"who": "alex"}


def test_an_adapter_cannot_reach_state_or_processes():
    """Invariants 29/34: adapters describe, they do not act or interpret."""
    adapter = ManualAdapter()
    for forbidden in (
        "state_store",
        "runtime",
        "process_store",
        "observe",
        "propose_delta",
        "spawn",
    ):
        assert not hasattr(adapter, forbidden)


def test_a_custom_adapter_id_partitions_the_key_space():
    a = ManualAdapter("import_job").envelope(
        event_type="row_loaded", source_event_key="1"
    )
    assert a.adapter_id == "import_job"


async def test_manual_ingest_then_redelivery(tmp_path):
    runtime = Runtime(tmp_path / "manual.db")
    service = ingress(runtime)
    adapter = ManualAdapter()

    def envelope():
        return adapter.envelope(
            event_type="human_message",
            source_event_key="demo-001",
            payload={"text": "D1のCD解析が完了しました"},
        )

    first = await service.ingest(envelope())
    second = await service.ingest(envelope())

    assert first.status is IngressStatus.ACCEPTED
    assert second.status is IngressStatus.DUPLICATE
    assert len(runtime.event_store.all()) == 1
    runtime.close()


# --- CLI -------------------------------------------------------------------


def test_the_cli_requires_an_idempotency_key():
    """Even a human at a terminal must say which occurrence this is."""
    parser = build_parser()
    args = parser.parse_args(
        ["--db", "x.db", "--event-type", "human_message", "--source-event-key", "k1"]
    )
    assert args.source_event_key == "k1"
    assert args.payload == "{}"


async def test_cli_run_ingests_once_and_reports_duplicates(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    argv = [
        "--db",
        db,
        "--event-type",
        "human_message",
        "--source-event-key",
        "demo-001",
        "--payload",
        json.dumps({"text": "hello"}),
    ]
    parser = build_parser()

    assert await run(parser.parse_args(argv)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "ACCEPTED"
    assert first["duplicate"] is False

    assert await run(parser.parse_args(argv)) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "DUPLICATE"
    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]

    runtime = Runtime(db)
    assert len(runtime.event_store.all()) == 1
    runtime.close()


def test_cli_main_rejects_malformed_payload(tmp_path, capsys):
    code = main(
        [
            "--db",
            str(tmp_path / "cli.db"),
            "--event-type",
            "human_message",
            "--source-event-key",
            "k",
            "--payload",
            "not json",
        ]
    )
    assert code == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_main_rejects_non_object_payload(tmp_path, capsys):
    code = main(
        [
            "--db",
            str(tmp_path / "cli.db"),
            "--event-type",
            "human_message",
            "--source-event-key",
            "k",
            "--payload",
            "[1, 2]",
        ]
    )
    assert code == 2
    assert "must be a JSON object" in capsys.readouterr().err


def test_cli_main_reports_a_rejected_envelope(tmp_path, capsys):
    code = main(
        [
            "--db",
            str(tmp_path / "cli.db"),
            "--event-type",
            "",
            "--source-event-key",
            "k",
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "REJECTED"
