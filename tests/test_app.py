"""The Project-centered runnable application entry point."""

from __future__ import annotations

import json

import pytest

from nexus_seed.app import (
    AppSettings,
    ApplicationConfigurationError,
    build_parser,
    build_runtime,
    run,
)


_ENV_NAMES = (
    "NEXUS_SEED_DATA_DIR",
    "NEXUS_SEED_WEBHOOK_HOST",
    "NEXUS_SEED_WEBHOOK_PORT",
    "NEXUS_SEED_WEBHOOK_TOKEN",
    "NEXUS_SEED_TICK_SECONDS",
    "NEXUS_SEED_LOG_LEVEL",
    "NEXUS_SEED_CONTROL_IDENTITY",
    "NEXUS_SEED_OPERATOR_ID",
    "NEXUS_SEED_COCKPIT_ENABLED",
    "NEXUS_SEED_KNOWLEDGE_LOOP_ENABLED",
    "NEXUS_SEED_KNOWLEDGE_POLL_SECONDS",
    "NEXUS_SEED_LLM_ENABLED",
    "NEXUS_SEED_LLM_PROVIDER",
    "NEXUS_SEED_LLM_BASE_URL",
    "NEXUS_SEED_LLM_MODEL",
    "NEXUS_SEED_LLM_MAX_TOKENS",
    "NEXUS_SEED_LLM_TIMEOUT_SECONDS",
    "NEXUS_SEED_LLM_API_KEY_ENV",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_application_environment(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def write_env(path, data_dir, *, host="127.0.0.1", token="test-token"):
    path.write_text(
        "\n".join(
            [
                f"NEXUS_SEED_DATA_DIR={data_dir}",
                f"NEXUS_SEED_WEBHOOK_HOST={host}",
                "NEXUS_SEED_WEBHOOK_PORT=8787",
                f"NEXUS_SEED_WEBHOOK_TOKEN={token}",
                "NEXUS_SEED_TICK_SECONDS=0.1",
                "NEXUS_SEED_LOG_LEVEL=WARNING",
                "NEXUS_SEED_LLM_ENABLED=false",
            ]
        ),
        encoding="utf-8",
    )


def test_build_runtime_bootstraps_only_the_project_application_stack(tmp_path):
    env_file = tmp_path / ".env"
    data_dir = tmp_path / "data"
    write_env(env_file, data_dir)
    settings = AppSettings.from_env(env_file)

    runtime = build_runtime(settings, env_file=env_file)
    try:
        names = {definition.name for definition in runtime.process_store.all_definitions()}
        assert "route_request_to_project" in names
        assert "resource_indexer" in names
        assert "extract_resource" in names
        assert "watch_files" in names
        assert "interpret_resource" not in names
        assert "interpret_event" not in names
        assert "compose_work_plan" not in names
        assert "analyze_capability_gap" not in names
        assert "review_human_work" not in names
        assert "local_file" not in runtime.backends
        assert "llm" not in runtime.backends
        assert runtime.project_orchestrator is not None
    finally:
        runtime.close()


def test_project_orchestrator_wires_the_knowledge_loop_and_local_inbox(tmp_path):
    env_file = tmp_path / ".env"
    data_dir = tmp_path / "data"
    write_env(env_file, data_dir)
    settings = AppSettings.from_env(env_file)

    runtime = build_runtime(settings, env_file=env_file)
    try:
        assert runtime.project_orchestrator is not None
        assert runtime.knowledge_loop is not None
        assert "knowledge_local_file" in runtime.adapters
        assert runtime.knowledge_loop.world_view() == {}
    finally:
        runtime.close()


async def test_once_recovers_drains_and_prints_status(tmp_path, capsys):
    env_file = tmp_path / ".env"
    data_dir = tmp_path / "data"
    write_env(env_file, data_dir)
    args = build_parser().parse_args(["--env-file", str(env_file), "--once"])

    assert await run(args) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "idle"
    assert report["database"] == str(data_dir / "nexus_seed.db")
    assert report["llm_enabled"] is False


def test_remote_listen_requires_webhook_token(tmp_path):
    env_file = tmp_path / ".env"
    write_env(env_file, tmp_path / "data", host="0.0.0.0", token="")

    with pytest.raises(ApplicationConfigurationError, match="TOKEN is required"):
        AppSettings.from_env(env_file)


def test_cockpit_defaults_on_and_can_be_disabled(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    write_env(env_file, tmp_path / "data")
    assert AppSettings.from_env(env_file).cockpit_enabled is True

    monkeypatch.setenv("NEXUS_SEED_COCKPIT_ENABLED", "false")
    assert AppSettings.from_env(env_file).cockpit_enabled is False


def test_once_and_llm_check_are_mutually_exclusive():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--once", "--check-llm"])


def test_operational_subcommands_accept_connection_options_after_command():
    args = build_parser().parse_args(
        ["task", "analyze this", "--port", "9999", "--source-key", "task-1"]
    )

    assert args.command == "task"
    assert args.text == "analyze this"
    assert args.port == 9999
    assert args.source_key == "task-1"
