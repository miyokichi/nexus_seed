"""`nexus-seed project` — the Project Orchestrator's command-line entry point.

`nexus-seed task` stays what it was (the durable Goal/Work runtime); this is the
separate door into the orchestrator, and the two do not share a database row.
"""

from __future__ import annotations

import json

import pytest

from nexus_seed.app import AppSettings, build_parser, build_runtime, run
from nexus_seed.orchestrator import ProjectOrchestrator, ProjectStatus

_ENV_NAMES = (
    "NEXUS_SEED_DATA_DIR",
    "NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED",
    "NEXUS_SEED_LLM_ENABLED",
    "NEXUS_SEED_LLM_PROVIDER",
    "NEXUS_SEED_LLM_BASE_URL",
    "NEXUS_SEED_LLM_MODEL",
    "NEXUS_SEED_LLM_API_KEY_ENV",
    "NEXUS_SEED_A2A_ENABLED",
    "NEXUS_SEED_A2A_CONFIG",
    "NEXUS_SEED_SKILL_ROOTS",
    "NEXUS_SEED_PROJECT_AGENT_RUNTIME",
    "NEXUS_SEED_PROJECT_AGENT_URL",
    "NEXUS_SEED_PROJECT_AGENT_TOKEN_ENV",
    "NEXUS_SEED_PROJECT_AGENT_TIMEOUT_SECONDS",
    "NEXUS_SEED_PROJECT_AGENT_POLL_SECONDS",
    "NEXUS_SEED_PROJECT_WORKSPACE",
)

REQUEST = "samples/sample_sales.csvを分析して2026年7月の売上低下原因を調べて"


@pytest.fixture(autouse=True)
def clean_project_environment(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def write_env(path, data_dir, **extra):
    values = {
        "NEXUS_SEED_DATA_DIR": data_dir,
        "NEXUS_SEED_WEBHOOK_HOST": "127.0.0.1",
        "NEXUS_SEED_WEBHOOK_PORT": "8787",
        "NEXUS_SEED_LOG_LEVEL": "WARNING",
        "NEXUS_SEED_LLM_ENABLED": "false",
        "NEXUS_SEED_PROJECT_AGENT_RUNTIME": "in_process",
        **extra,
    }
    path.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()), encoding="utf-8"
    )
    return path


async def test_project_cli_creates_project(tmp_path, capsys):
    env_file = write_env(tmp_path / ".env", tmp_path / "data")
    args = build_parser().parse_args(["--env-file", str(env_file), "project", REQUEST])

    assert await run(args) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Routing: CREATE_PROJECT"
    assert lines[1].startswith("Project: project-")
    assert lines[2].startswith("Agent:   agent-")
    assert lines[3] == "Status:  ACTIVE"

    # The project is durable: it is read back from SQLite, not from the command.
    orchestrator = ProjectOrchestrator(tmp_path / "data" / "nexus_seed.db")
    try:
        [project] = orchestrator.projects.all()
        assert project.goal == REQUEST
        assert project.status is ProjectStatus.ACTIVE
        assert project.assigned_agent_id is not None
        agent = orchestrator.agents.for_project(project.id)
        assert agent is not None and agent.runtime == "in_process"
    finally:
        orchestrator.close()


async def test_project_cli_emits_json_on_request(tmp_path, capsys):
    env_file = write_env(tmp_path / ".env", tmp_path / "data")
    args = build_parser().parse_args(
        ["--env-file", str(env_file), "project", REQUEST, "--priority", "5", "--json"]
    )

    assert await run(args) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["routing"]["action"] == "CREATE_PROJECT"
    assert report["project"]["priority"] == 5
    assert report["agent"]["project_id"] == report["project"]["id"]


async def test_project_and_task_are_separate_entry_points():
    parser = build_parser()

    assert parser.parse_args(["project", "x"]).command == "project"
    assert parser.parse_args(["task", "x"]).command == "task"
    # `task` still speaks to the durable runtime over the webhook, so it keeps
    # its own deduplication key and gains none of the orchestrator's options.
    assert hasattr(parser.parse_args(["task", "x"]), "source_key")
    assert not hasattr(parser.parse_args(["project", "x"]), "source_key")


async def test_project_cli_refuses_an_a2a_runtime_without_a_url(tmp_path):
    env_file = write_env(
        tmp_path / ".env", tmp_path / "data", NEXUS_SEED_PROJECT_AGENT_RUNTIME="a2a"
    )
    args = build_parser().parse_args(["--env-file", str(env_file), "project", REQUEST])

    with pytest.raises(Exception, match="NEXUS_SEED_PROJECT_AGENT_URL"):
        await run(args)


async def test_the_orchestrator_takes_over_message_interpretation(tmp_path):
    """One message, one LLM answer: the flag switches the path, it does not add one.

    Running both against a real local model doubled the wait for every request
    and the two prompts interfered, so with the orchestrator on a human_message
    is Project work and nothing else.
    """
    env_file = write_env(
        tmp_path / ".env",
        tmp_path / "data",
        NEXUS_SEED_PROJECT_ORCHESTRATOR_ENABLED="true",
        NEXUS_SEED_LLM_ENABLED="true",
        NEXUS_SEED_LLM_PROVIDER="openai_compatible",
        NEXUS_SEED_LLM_BASE_URL="http://127.0.0.1:1/v1",
        NEXUS_SEED_LLM_MODEL="test-model",
    )
    settings = AppSettings.from_env(env_file)
    runtime = build_runtime(settings, env_file=env_file)
    try:
        routed = runtime.get_definition("route_request_to_project", "1")
        interpreter = runtime.get_definition("interpret_event_llm", "1")

        assert routed is not None
        assert routed.trigger_event_types == ("human_message",)
        # Still registered, still bound — only stood down, so turning the flag
        # off restores it exactly.
        assert interpreter is not None
        assert interpreter.trigger_event_types == ()
        assert runtime.project_orchestrator is not None
    finally:
        runtime.close()


async def test_message_interpretation_is_untouched_without_the_flag(tmp_path):
    env_file = write_env(
        tmp_path / ".env",
        tmp_path / "data",
        NEXUS_SEED_LLM_ENABLED="true",
        NEXUS_SEED_LLM_PROVIDER="openai_compatible",
        NEXUS_SEED_LLM_BASE_URL="http://127.0.0.1:1/v1",
        NEXUS_SEED_LLM_MODEL="test-model",
    )
    settings = AppSettings.from_env(env_file)
    runtime = build_runtime(settings, env_file=env_file)
    try:
        interpreter = runtime.get_definition("interpret_event_llm", "1")
        assert interpreter.trigger_event_types == ("human_message",)
        assert runtime.get_definition("route_request_to_project", "1") is None
        assert runtime.project_orchestrator is None
    finally:
        runtime.close()
