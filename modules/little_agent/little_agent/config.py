from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _path_list(value: str | None) -> tuple[Path, ...]:
    if not value:
        return ()
    return tuple(Path(item.strip()).resolve() for item in value.split(os.pathsep) if item.strip())


def builtin_skills_dir() -> Path:
    """The skill library shipped inside the installed package.

    Skills are a runtime resource, not workspace data: they live under
    ``little_agent/builtin_skills/`` and are installed by pip along with the
    code, so pointing the agent at some other directory to work in never costs
    it its skills.
    """

    return (Path(__file__).resolve().parent / "builtin_skills").resolve()


def resolve_skill_library(raw: str | None, workspace: Path) -> Path:
    """Where to load skills from, most specific source first.

    1. ``LITTLE_AGENT_SKILL_LIBRARY_DIR`` when set — an explicit choice always
       wins, and is honoured even if it does not exist yet (so a typo surfaces
       as an empty library rather than being silently ignored).
    2. ``<workspace>/skills`` when that directory exists — a project may carry
       its own library.
    3. The built-in library shipped with the package.
    """

    if raw and raw.strip():
        path = Path(raw.strip()).expanduser()
        return (path if path.is_absolute() else workspace / path).resolve()
    workspace_library = (workspace / "skills").resolve()
    if workspace_library.is_dir():
        return workspace_library
    return builtin_skills_dir()


def describe_skill_library(config: "AgentConfig") -> str:
    """Where the skills came from, so an unexpected library is visible at a glance.

    The label names which rung of the resolution order won, which is what tells
    an operator whether their setting took effect.
    """

    if config.skill_library_dir == builtin_skills_dir():
        origin = "built-in"
    elif config.skill_library_dir == (config.workspace / "skills").resolve():
        origin = "workspace"
    else:
        origin = "configured"
    return f"{config.skill_library_dir} ({origin})"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str
    workspace: Path
    require_confirmation: bool
    openai_api_key: str | None
    openai_base_url: str
    readable_paths: tuple[Path, ...] = ()
    writable_paths: tuple[Path, ...] = ()
    max_tool_steps: int = 5
    enable_logging: bool = False
    log_dir: Path | None = None
    llm_timeout_seconds: int = 60
    commands_dir: Path = field(default_factory=lambda: (Path.cwd() / "commands").resolve())
    global_commands_dir: Path = field(default_factory=lambda: Path.home() / ".little_agent" / "commands")
    skill_library_dir: Path = field(default_factory=builtin_skills_dir)
    agents_dir: Path = field(default_factory=lambda: (Path.cwd() / "agents").resolve())
    active_agent: str | None = None
    stop_hotkey: str = "<ctrl>+<alt>+q"
    # Persistent memory (used only when a chat session runs with memory on; see
    # little_agent.memory). A2A tasks and `chat --no-memory` never read these.
    global_memory_path: Path = field(
        default_factory=lambda: Path.home() / ".little_agent" / "memory.md"
    )
    global_profile_path: Path = field(
        default_factory=lambda: Path.home() / ".little_agent" / "profile.md"
    )
    enable_auto_learning: bool = True
    # How deep delegate_task may nest sub-agents (0 disables delegation entirely).
    max_delegation_depth: int = 2
    # How many subtasks delegate_tasks runs concurrently.
    max_parallel_delegations: int = 4

    @classmethod
    def from_env(cls) -> "AgentConfig":
        load_dotenv()
        workspace = Path(os.getenv("LITTLE_AGENT_WORKSPACE", ".")).resolve()
        configured_log_dir = Path(os.getenv("LITTLE_AGENT_LOG_DIR", "logs"))
        log_dir = configured_log_dir if configured_log_dir.is_absolute() else workspace / configured_log_dir
        log_dir = log_dir.resolve()
        configured_commands_dir = Path(os.getenv("LITTLE_AGENT_COMMANDS_DIR", "commands"))
        commands_dir = configured_commands_dir if configured_commands_dir.is_absolute() else workspace / configured_commands_dir
        commands_dir = commands_dir.resolve()
        raw_global_commands = os.getenv("LITTLE_AGENT_GLOBAL_COMMANDS_DIR")
        global_commands_dir = Path(raw_global_commands).resolve() if raw_global_commands else Path.home() / ".little_agent" / "commands"
        skill_library_dir = resolve_skill_library(
            os.getenv("LITTLE_AGENT_SKILL_LIBRARY_DIR"), workspace
        )
        configured_agents_dir = Path(os.getenv("LITTLE_AGENT_AGENTS_DIR", "agents"))
        agents_dir = configured_agents_dir if configured_agents_dir.is_absolute() else workspace / configured_agents_dir
        agents_dir = agents_dir.resolve()
        active_agent = os.getenv("LITTLE_AGENT_AGENT") or None
        raw_global_memory = os.getenv("LITTLE_AGENT_GLOBAL_MEMORY_PATH")
        global_memory_path = (
            Path(raw_global_memory).resolve()
            if raw_global_memory
            else Path.home() / ".little_agent" / "memory.md"
        )
        raw_global_profile = os.getenv("LITTLE_AGENT_GLOBAL_PROFILE_PATH")
        global_profile_path = (
            Path(raw_global_profile).resolve()
            if raw_global_profile
            else Path.home() / ".little_agent" / "profile.md"
        )
        return cls(
            model=os.getenv("LITTLE_AGENT_MODEL", "gpt-4.1-mini"),
            workspace=workspace,
            readable_paths=_path_list(os.getenv("LITTLE_AGENT_READABLE_PATHS")),
            writable_paths=_path_list(os.getenv("LITTLE_AGENT_WRITABLE_PATHS")),
            require_confirmation=_as_bool(os.getenv("LITTLE_AGENT_REQUIRE_CONFIRMATION"), True),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            max_tool_steps=int(os.getenv("LITTLE_AGENT_MAX_TOOL_STEPS", "5")),
            enable_logging=_as_bool(os.getenv("LITTLE_AGENT_ENABLE_LOGGING"), True),
            log_dir=log_dir,
            llm_timeout_seconds=int(os.getenv("LITTLE_AGENT_TIMEOUT_SECONDS", "60")),
            commands_dir=commands_dir,
            global_commands_dir=global_commands_dir,
            skill_library_dir=skill_library_dir,
            agents_dir=agents_dir,
            active_agent=active_agent,
            stop_hotkey=os.getenv("LITTLE_AGENT_STOP_HOTKEY", "<ctrl>+<alt>+q"),
            global_memory_path=global_memory_path,
            global_profile_path=global_profile_path,
            enable_auto_learning=_as_bool(os.getenv("LITTLE_AGENT_AUTO_LEARNING"), True),
            max_delegation_depth=int(os.getenv("LITTLE_AGENT_MAX_DELEGATION_DEPTH", "2")),
            max_parallel_delegations=max(
                1, int(os.getenv("LITTLE_AGENT_MAX_PARALLEL_DELEGATIONS", "4"))
            ),
        )
