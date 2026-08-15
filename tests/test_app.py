"""The runnable Phase 1–5D application entry point."""

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


def test_build_runtime_bootstraps_the_complete_stack(tmp_path):
    env_file = tmp_path / ".env"
    data_dir = tmp_path / "data"
    write_env(env_file, data_dir)
    settings = AppSettings.from_env(env_file)

    runtime = build_runtime(settings, env_file=env_file)
    try:
        names = {definition.name for definition in runtime.process_store.all_definitions()}
        assert "interpret_event" in names
        assert "compose_work_plan" in names
        assert "watch_files" in names
        assert "analyze_capability_gap" in names
        assert "advance_capability_acquisition" in names
        assert "activate_installed_extension" in names
        assert "local_file" in runtime.backends
        assert "llm" not in runtime.backends
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


def test_once_and_llm_check_are_mutually_exclusive():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--once", "--check-llm"])
