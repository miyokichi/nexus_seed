"""External Agent Runtime configuration: providers, bindings and skill roots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_seed.federation_config import (
    DEFAULT_SKILL_ROOTS,
    FederationConfigurationError,
    FederationSettings,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "a2a.example.json"


def config(**overrides):
    raw = {
        "providers": {"observer_agent": {"type": "a2a", "url": "http://127.0.0.1:8801"}},
        "bindings": {"world_event_interpretation": {"provider": "observer_agent"}},
    }
    raw.update(overrides)
    return raw


def test_shipped_example_config_is_valid():
    settings = FederationSettings.from_dict(
        json.loads(EXAMPLE.read_text(encoding="utf-8"))
    )

    assert sorted(p.name for p in settings.providers) == [
        "observer_agent",
        "planner_agent",
    ]
    assert settings.bindings["world_event_interpretation"] == "observer_agent"


def test_binding_to_an_unknown_provider_is_refused():
    raw = config(bindings={"goal_evaluation": {"provider": "typo_agent"}})

    with pytest.raises(FederationConfigurationError, match="unknown provider"):
        FederationSettings.from_dict(raw)


def test_a_provider_needs_a_url():
    with pytest.raises(FederationConfigurationError, match="needs a url"):
        FederationSettings.from_dict({"providers": {"agent": {"type": "a2a"}}})


def test_only_the_a2a_transport_is_supported():
    raw = {"providers": {"agent": {"type": "grpc", "url": "http://127.0.0.1:1"}}}

    with pytest.raises(FederationConfigurationError, match="unsupported type"):
        FederationSettings.from_dict(raw)


def test_a_non_http_url_is_refused_when_the_endpoint_is_built():
    settings = FederationSettings.from_dict(
        {"providers": {"agent": {"url": "ftp://127.0.0.1:21"}}}
    )

    with pytest.raises(FederationConfigurationError, match="http"):
        settings.providers[0].endpoint()


def test_default_skill_roots_are_project_local_then_user_global():
    settings = FederationSettings.from_dict(config())

    assert settings.skill_roots == DEFAULT_SKILL_ROOTS


def test_skill_roots_come_from_the_environment_when_unconfigured(monkeypatch, tmp_path):
    import os

    monkeypatch.setenv(
        "NEXUS_SEED_SKILL_ROOTS", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    )

    settings = FederationSettings.from_dict(config())

    assert settings.skill_roots == (str(tmp_path / "a"), str(tmp_path / "b"))


def test_duplicate_policy_must_be_a_known_rule():
    raw = config(skills={"on_duplicate": "whatever"})

    with pytest.raises(FederationConfigurationError, match="on_duplicate"):
        FederationSettings.from_dict(raw)


def test_settings_are_disabled_unless_the_environment_enables_them(monkeypatch, tmp_path):
    path = tmp_path / "a2a.json"
    path.write_text(json.dumps(config()), encoding="utf-8")
    monkeypatch.delenv("NEXUS_SEED_A2A_ENABLED", raising=False)
    monkeypatch.setenv("NEXUS_SEED_A2A_CONFIG", str(path))

    settings = FederationSettings.from_env(tmp_path / "absent.env")

    assert settings.enabled is False
    assert [p.name for p in settings.providers] == ["observer_agent"]


def test_enabled_settings_load_the_configured_file(monkeypatch, tmp_path):
    path = tmp_path / "a2a.json"
    path.write_text(json.dumps(config()), encoding="utf-8")
    monkeypatch.setenv("NEXUS_SEED_A2A_ENABLED", "true")
    monkeypatch.setenv("NEXUS_SEED_A2A_CONFIG", str(path))

    settings = FederationSettings.from_env(tmp_path / "absent.env")

    assert settings.enabled is True
    assert settings.providers[0].url == "http://127.0.0.1:8801"


def test_a_missing_config_file_is_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_SEED_A2A_CONFIG", str(tmp_path / "nope.json"))

    with pytest.raises(FederationConfigurationError, match="does not exist"):
        FederationSettings.from_env(tmp_path / "absent.env")


def test_a_malformed_config_file_names_itself(monkeypatch, tmp_path):
    path = tmp_path / "a2a.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("NEXUS_SEED_A2A_CONFIG", str(path))

    with pytest.raises(FederationConfigurationError, match="invalid A2A config"):
        FederationSettings.from_env(tmp_path / "absent.env")


def test_no_config_file_still_yields_usable_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_SEED_A2A_CONFIG", raising=False)
    monkeypatch.delenv("NEXUS_SEED_SKILL_ROOTS", raising=False)
    monkeypatch.setenv("NEXUS_SEED_A2A_ENABLED", "false")

    settings = FederationSettings.from_env(tmp_path / "absent.env")

    assert settings.providers == ()
    assert settings.skill_roots == DEFAULT_SKILL_ROOTS
