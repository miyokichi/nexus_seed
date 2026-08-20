"""Configuration says which model is which, without being read carefully.

Two language models are involved and their settings look alike: the one NEXUS
SEED thinks with, and the one that does the delegated work.  Only the first is
configured here at all.  These tests pin that Skills have one owner, and that
`nexus-seed config` reports the resolved settings grouped by what they are for.
"""

from __future__ import annotations

import json
import os

import pytest

from nexus_seed.app import build_parser, run
from nexus_seed.config_report import build_report, format_report
from nexus_seed.skills_config import (
    DEFAULT_SKILL_ROOTS,
    SkillConfigurationError,
    SkillSettings,
)


ENV_VARS = (
    "NEXUS_SEED_LLM_ENABLED",
    "NEXUS_SEED_LLM_PROVIDER",
    "NEXUS_SEED_LLM_MODEL",
    "NEXUS_SEED_LLM_BASE_URL",
    "NEXUS_SEED_LLM_API_KEY_ENV",
    "NEXUS_SEED_SKILL_ROOTS",
    "NEXUS_SEED_SKILLS_STRICT",
    "NEXUS_SEED_SKILLS_ON_DUPLICATE",
    "NEXUS_SEED_PROJECT_AGENT_RUNTIME",
    "NEXUS_SEED_PROJECT_AGENT_URL",
    "NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV",
    "NEXUS_SEED_PROJECT_WORKSPACE",
    "NEXUS_SEED_DATA_DIR",
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


# --- skills have one owner ------------------------------------------------------


def test_skill_roots_default_when_unset(tmp_path):
    settings = SkillSettings.from_env(tmp_path / "missing.env")

    assert settings.roots == DEFAULT_SKILL_ROOTS
    assert settings.strict is False
    assert settings.on_duplicate == "override"


def test_skill_roots_come_from_the_environment(tmp_path, monkeypatch):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    monkeypatch.setenv("NEXUS_SEED_SKILL_ROOTS", os.pathsep.join([str(first), str(second)]))

    settings = SkillSettings.from_env(tmp_path / "missing.env")

    assert settings.roots == (str(first), str(second))
    # A configured root that does not exist is reported, not hidden.
    assert settings.described_roots() == [(str(first), True), (str(second), False)]


def test_a_blank_setting_is_not_a_root(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_SEED_SKILL_ROOTS", os.pathsep * 3)

    assert SkillSettings.from_env(tmp_path / "missing.env").roots == DEFAULT_SKILL_ROOTS


def test_an_unknown_duplicate_policy_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_SEED_SKILLS_ON_DUPLICATE", "whichever")

    with pytest.raises(SkillConfigurationError, match="ON_DUPLICATE"):
        SkillSettings.from_env(tmp_path / "missing.env")


def test_the_project_agent_has_no_skill_setting_here(tmp_path, monkeypatch):
    """A Project is delegated as a goal, not as a method.

    The Agent reads its own skills from its own configuration file, so NEXUS
    SEED's roots must not reach it — not even by accident.
    """

    from nexus_seed.orchestrator_config import ProjectAgentSettings

    root = tmp_path / "nexus-seed-skills"
    root.mkdir()
    monkeypatch.setenv("NEXUS_SEED_SKILL_ROOTS", str(root))

    settings = ProjectAgentSettings.from_env(tmp_path / "missing.env")

    assert not hasattr(settings, "skills")
    assert not hasattr(settings, "skill_roots")


def test_nothing_about_skills_reaches_a_delegated_agent(tmp_path):
    """The delegation group must not describe what the Agent can do."""

    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_PROJECT_AGENT_RUNTIME="a2a",
        NEXUS_SEED_PROJECT_AGENT_URL="http://127.0.0.1:8801",
    )

    delegation = build_report(env_file)["groups"][2]

    assert not any("SKILL" in name.upper() for name in delegation["settings"])
    assert any("own configuration file" in note for note in delegation["notes"])


# --- the report -----------------------------------------------------------------


def test_the_report_separates_the_two_models(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "s3cret")
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="true",
        NEXUS_SEED_LLM_PROVIDER="anthropic",
        NEXUS_SEED_LLM_MODEL="claude-sonnet-4-5",
        NEXUS_SEED_LLM_API_KEY_ENV="MY_KEY",
    )

    report = build_report(env_file)
    names = [group["name"] for group in report["groups"]]

    assert names == ["NEXUS SEED's own reasoning", "NEXUS SEED's own Skills", "Delegation"]
    reasoning, _skills, delegation = report["groups"]
    assert reasoning["settings"]["NEXUS_SEED_LLM_MODEL"] == "claude-sonnet-4-5"
    # The delegated model is not configured here, and the report says so.
    assert any("not how the work is done" in note for note in delegation["notes"])
    assert not any("LLM" in name for name in delegation["settings"])


def test_a_key_is_reported_as_set_never_printed(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "s3cret")
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="true",
        NEXUS_SEED_LLM_PROVIDER="anthropic",
        NEXUS_SEED_LLM_API_KEY_ENV="MY_KEY",
    )

    rendered = format_report(build_report(env_file))

    assert "MY_KEY" in rendered
    assert "set" in rendered
    assert "s3cret" not in rendered


def test_an_empty_key_is_reported_as_empty(tmp_path):
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_LLM_PROVIDER="anthropic",
        NEXUS_SEED_LLM_API_KEY_ENV="MY_KEY",
    )

    rendered = format_report(build_report(env_file))

    assert "EMPTY" in rendered


def test_the_report_lists_the_skills_it_actually_found(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_SEED_SKILL_ROOTS", str(tmp_path / "nowhere"))
    env_file = write_env(tmp_path / ".env", NEXUS_SEED_LLM_ENABLED="false")

    skills = build_report(env_file)["groups"][1]

    assert any("0 Skill(s) loaded" in note for note in skills["notes"])
    assert skills["settings"][f"  {tmp_path / 'nowhere'}"] == "missing"


def test_the_a2a_runtime_reports_where_it_delegates(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "t")
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_PROJECT_AGENT_RUNTIME="a2a",
        NEXUS_SEED_PROJECT_AGENT_URL="http://127.0.0.1:8801",
        NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV="AGENT_TOKEN",
    )

    delegation = build_report(env_file)["groups"][2]

    assert delegation["settings"]["NEXUS_SEED_PROJECT_AGENT_URL"] == "http://127.0.0.1:8801"
    assert delegation["settings"]["  AGENT_TOKEN"] == "set"


def test_the_in_process_runtime_says_it_calls_no_model(tmp_path):
    env_file = write_env(tmp_path / ".env", NEXUS_SEED_LLM_ENABLED="false")

    delegation = build_report(env_file)["groups"][2]

    assert any("calls no model" in note for note in delegation["notes"])
    assert "NEXUS_SEED_PROJECT_AGENT_URL" not in delegation["settings"]


# --- the command ----------------------------------------------------------------


async def test_config_command_prints_the_groups(tmp_path, capsys):
    env_file = write_env(
        tmp_path / ".env",
        NEXUS_SEED_LLM_ENABLED="false",
        NEXUS_SEED_DATA_DIR=str(tmp_path / "data"),
    )
    args = build_parser().parse_args(["--env-file", env_file, "config"])

    assert await run(args) == 0

    out = capsys.readouterr().out
    assert "NEXUS SEED's own reasoning" in out
    assert "NEXUS SEED's own Skills" in out
    assert "Delegation" in out
    # Reporting starts nothing.
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
    assert payload["env_file"] == env_file
    assert [group["name"] for group in payload["groups"]] == [
        "NEXUS SEED's own reasoning",
        "NEXUS SEED's own Skills",
        "Delegation",
    ]
