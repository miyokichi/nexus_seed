"""Environment-file configuration for the real LLM integration."""

from __future__ import annotations

import os

import pytest

from nexus_seed.llm_config import (
    LLMConfigurationError,
    LLMSettings,
    configure_llm,
    load_env_file,
)
from nexus_seed.runtime import Runtime


_NAMES = (
    "NEXUS_SEED_LLM_ENABLED",
    "NEXUS_SEED_LLM_PROVIDER",
    "NEXUS_SEED_LLM_MODEL",
    "NEXUS_SEED_LLM_BASE_URL",
    "NEXUS_SEED_LLM_MAX_TOKENS",
    "NEXUS_SEED_LLM_TIMEOUT_SECONDS",
    "NEXUS_SEED_LLM_API_KEY_ENV",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "TEST_ANTHROPIC_KEY",
)


@pytest.fixture(autouse=True)
def clean_llm_environment(monkeypatch):
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)


def test_settings_load_from_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NEXUS_SEED_LLM_ENABLED=true",
                "NEXUS_SEED_LLM_PROVIDER=anthropic",
                'NEXUS_SEED_LLM_MODEL="test-model"',
                "NEXUS_SEED_LLM_MAX_TOKENS=4096",
                "NEXUS_SEED_LLM_API_KEY_ENV=TEST_ANTHROPIC_KEY",
                "TEST_ANTHROPIC_KEY=secret",
            ]
        ),
        encoding="utf-8",
    )

    settings = LLMSettings.from_env(env_file)

    assert settings.enabled is True
    assert settings.model == "test-model"
    assert settings.max_tokens == 4096
    assert settings.api_key_env == "TEST_ANTHROPIC_KEY"


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NEXUS_SEED_LLM_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setenv("NEXUS_SEED_LLM_MODEL", "deployed-model")

    load_env_file(env_file)

    assert os.environ["NEXUS_SEED_LLM_MODEL"] == "deployed-model"


def test_openai_compatible_settings_allow_local_server_without_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NEXUS_SEED_LLM_ENABLED=true",
                "NEXUS_SEED_LLM_PROVIDER=openai-compatible",
                "NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1/",
                "NEXUS_SEED_LLM_MODEL=local-model",
                "NEXUS_SEED_LLM_API_KEY_ENV=OPENAI_API_KEY",
                "NEXUS_SEED_LLM_TIMEOUT_SECONDS=45.5",
            ]
        ),
        encoding="utf-8",
    )

    settings = LLMSettings.from_env(env_file)

    assert settings.enabled is True
    assert settings.provider == "openai_compatible"
    assert settings.base_url == "http://127.0.0.1:1234/v1/"
    assert settings.timeout_seconds == 45.5


def test_disabled_configuration_keeps_deterministic_fallbacks(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NEXUS_SEED_LLM_ENABLED=false\n", encoding="utf-8")
    runtime = Runtime(tmp_path / "disabled.db")
    try:
        assert configure_llm(runtime, env_file=env_file) is None
        assert runtime.llm_plan_selector is None
        assert runtime.llm_extension_proposer is None
        assert runtime.llm_construction_generator is None
        assert "llm" not in runtime.backends
    finally:
        runtime.close()


def test_enabled_configuration_wires_every_llm_role(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NEXUS_SEED_LLM_ENABLED=true",
                "NEXUS_SEED_LLM_PROVIDER=openai_compatible",
                "NEXUS_SEED_LLM_BASE_URL=http://127.0.0.1:1234/v1",
                "NEXUS_SEED_LLM_MODEL=test-model",
                "NEXUS_SEED_LLM_MAX_TOKENS=7777",
                "NEXUS_SEED_LLM_API_KEY_ENV=OPENAI_API_KEY",
            ]
        ),
        encoding="utf-8",
    )
    runtime = Runtime(tmp_path / "enabled.db")
    try:
        backend = configure_llm(runtime, env_file=env_file)

        assert backend is not None
        assert backend.provider == "openai_compatible"
        assert backend.base_url == "http://127.0.0.1:1234/v1"
        assert backend.model == "test-model"
        assert backend.max_tokens == 7777
        assert runtime.backends["llm"] is backend
        assert runtime.llm_plan_selector.backend is backend
        assert runtime.llm_extension_proposer.backend is backend
        assert runtime.llm_construction_generator.backend is backend
    finally:
        runtime.close()


def test_enabled_configuration_requires_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NEXUS_SEED_LLM_ENABLED=true\n", encoding="utf-8")

    with pytest.raises(LLMConfigurationError, match="ANTHROPIC_API_KEY is empty"):
        LLMSettings.from_env(env_file)
