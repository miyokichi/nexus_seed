"""Configuration clearly separates NEXUS SEED reasoning from delegation."""

from __future__ import annotations

import json

import pytest

from nexus_seed.app import build_parser, run
from nexus_seed.config_report import build_report, format_report


ENV_VARS = (
    "NEXUS_SEED_LLM_ENABLED",
    "NEXUS_SEED_LLM_PROVIDER",
    "NEXUS_SEED_LLM_MODEL",
    "NEXUS_SEED_LLM_BASE_URL",
    "NEXUS_SEED_LLM_API_KEY_ENV",
    "NEXUS_SEED_PROJECT_AGENT_RUNTIME",
    "NEXUS_SEED_PROJECT_AGENT_URL",
    "NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV",
    "NEXUS_SEED_PROJECT_WORKSPACE",
    "NEXUS_SEED_DATA_DIR",
    "NEXUS_SEED_SEMANTICA_SNAPSHOT",
    "NEXUS_SEED_SEMANTICA_ONTOLOGY",
    "NEXUS_SEED_SEMANTICA_INFER_RELATIONS",
    "AGENT_TOKEN",
    "MY_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def write_env(path, **values) -> str:
    path.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()),
        encoding="utf-8",
    )
    return str(path)


def test_report_separates_reasoning_delegation_and_knowledge_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "s3cret")
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="true",
        NEXUS_SEED_LLM_PROVIDER="anthropic",
        NEXUS_SEED_LLM_MODEL="claude-sonnet-4-5",
        NEXUS_SEED_LLM_API_KEY_ENV="MY_KEY",
    )

    report = build_report(env_file)
    assert [group["name"] for group in report["groups"]] == [
        "NEXUS SEED's own reasoning",
        "Delegation",
        "Knowledge backend",
    ]
    reasoning, delegation, knowledge = report["groups"]
    assert knowledge["settings"]["NEXUS_SEED_SEMANTICA_SNAPSHOT"] == "(none)"
    assert reasoning["settings"]["NEXUS_SEED_LLM_MODEL"] == "claude-sonnet-4-5"
    assert not any("SKILL" in name.upper() for name in delegation["settings"])
    assert "s3cret" not in format_report(report)


def test_a2a_runtime_reports_where_it_delegates(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "t")
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_PROJECT_AGENT_RUNTIME="a2a",
        NEXUS_SEED_PROJECT_AGENT_URL="http://127.0.0.1:8801",
        NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV="AGENT_TOKEN",
    )

    delegation = build_report(env_file)["groups"][1]
    assert delegation["settings"]["NEXUS_SEED_PROJECT_AGENT_URL"] == (
        "http://127.0.0.1:8801"
    )
    assert delegation["settings"]["  AGENT_TOKEN"] == "set"


def test_in_process_runtime_documents_reasoning_backend_reuse(tmp_path):
    env_file = write_env(tmp_path / ".env", NEXUS_SEED_LLM_ENABLED="false")

    delegation = build_report(env_file)["groups"][1]
    assert any("reuses" in note for note in delegation["notes"])
    assert "NEXUS_SEED_PROJECT_AGENT_URL" not in delegation["settings"]


async def test_config_command_prints_current_groups_without_starting_runtime(
    tmp_path, capsys
):
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_DATA_DIR=str(tmp_path / "data"),
    )
    args = build_parser().parse_args(["--env-file", env_file, "config"])

    assert await run(args) == 0
    out = capsys.readouterr().out
    assert "NEXUS SEED's own reasoning" in out
    assert "Delegation" in out
    assert "NEXUS SEED's own Skills" not in out
    assert not (tmp_path / "data" / "nexus_seed.db").exists()


async def test_config_command_emits_json(tmp_path, capsys):
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_DATA_DIR=str(tmp_path / "data"),
    )
    args = build_parser().parse_args(["--env-file", env_file, "config", "--json"])

    assert await run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [group["name"] for group in payload["groups"]] == [
        "NEXUS SEED's own reasoning",
        "Delegation",
        "Knowledge backend",
    ]
