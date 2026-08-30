"""Agent profiles: the capability contract of an agent, read from the filesystem.

A *profile* is a folder under ``agents/`` holding an ``agent.json``. It declares
what an agent may do — which skills it loads, which core tools it may call, its
model and step budget, and the description published on its A2A Agent Card.

Skills come from either place, so a profile can be a pure declaration or ship its
own skill folders:

* ``"skills": ["datetime", "excel_file"]`` in ``agent.json`` selects skills by
  name from the shared library (``skills/``).
* Skill folders dropped in ``agents/<name>/skills/`` are used as-is, and shadow a
  library skill of the same name.

Profiles are read-only here: Little Agent never writes them. Creating and
editing agents is the job of whoever operates the runtime.

This module is intentionally free of any dependency on ``Agent``/LLM code so it
can be imported anywhere (CLI, A2A server, tests) without import cycles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from little_agent.config import AgentConfig

AGENT_CONFIG_FILE = "agent.json"

# The library-wide mode is exposed as a built-in agent so that "run an agent" is
# the single model. These names are reserved and cannot be used for real agents.
DEFAULT_AGENT_NAME = "default"
RESERVED_NAMES = {"default", "library", "none", "-"}


def normalize_name(name: str) -> str:
    """Slugify an agent name."""

    lowered = name.strip().lower().replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    return normalized[:63]


def _safe_child(root: Path, name: str) -> Path:
    """Resolve ``root/name`` and refuse anything that escapes ``root``."""

    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"Invalid name: {name!r}")
    root = root.resolve()
    child = (root / name).resolve()
    if root not in [child, *child.parents]:
        raise ValueError(f"Path escaped the parent directory: {name!r}")
    return child


@dataclass(slots=True)
class AgentProfile:
    name: str
    dir: Path
    library_dir: Path
    description: str = ""
    model: str | None = None
    # Skill folder names selected from the library; None means "whatever is in
    # this profile's own skills/ folder".
    skills: list[str] | None = None
    core_tools: list[str] | None = None
    max_tool_steps: int | None = None
    require_confirmation: bool | None = None
    # ``builtin`` marks the virtual "default" agent (the whole library); it has
    # no agent.json on disk and loads every skill in the library.
    builtin: bool = False

    @property
    def own_skills_dir(self) -> Path:
        return self.dir / "skills"

    def skill_roots(self) -> list[Path]:
        """Directories to load skills from, most specific first."""

        if self.builtin:
            return [self.library_dir]
        roots = [self.own_skills_dir]
        if self.skills is not None:
            roots.append(self.library_dir)
        return roots

    def skill_names(self) -> set[str] | None:
        return set(self.skills) if self.skills is not None else None

    def enabled_skills(self) -> list[str]:
        """Skill folder names this profile actually resolves to."""

        found: set[str] = set()
        wanted = self.skill_names()
        for root in self.skill_roots():
            if not root.exists():
                continue
            for path in root.iterdir():
                if not path.is_dir() or not (path / "SKILL.md").exists():
                    continue
                if wanted is None or path.name in wanted:
                    found.add(path.name)
        return sorted(found)

    def core_tools_set(self) -> set[str] | None:
        return set(self.core_tools) if self.core_tools is not None else None

    def tool_allowed(self, name: str) -> bool:
        """Whether a core tool (including the delegation tools) is available."""

        allowed = self.core_tools_set()
        return allowed is None or name in allowed


def agent_dir(agents_dir: Path, name: str) -> Path:
    return _safe_child(agents_dir, normalize_name(name))


def list_agents(agents_dir: Path) -> list[str]:
    if not agents_dir.exists():
        return []
    return sorted(
        path.name
        for path in agents_dir.iterdir()
        if path.is_dir() and (path / AGENT_CONFIG_FILE).exists()
    )


def load_profile(agents_dir: Path, name: str, library_dir: Path | None = None) -> AgentProfile:
    directory = agent_dir(agents_dir, name)
    config_path = directory / AGENT_CONFIG_FILE
    if not config_path.exists():
        raise FileNotFoundError(f"No such agent: {normalize_name(name)}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} must contain a JSON object.")
    skills = data.get("skills")
    core_tools = data.get("core_tools")
    max_steps = data.get("max_tool_steps")
    confirm = data.get("require_confirmation")
    return AgentProfile(
        name=str(data.get("name") or directory.name),
        dir=directory,
        library_dir=(library_dir or agents_dir.parent / "skills").resolve(),
        description=str(data.get("description") or ""),
        model=(str(data["model"]) if data.get("model") else None),
        skills=[str(item) for item in skills] if isinstance(skills, list) else None,
        core_tools=[str(item) for item in core_tools] if isinstance(core_tools, list) else None,
        max_tool_steps=int(max_steps) if max_steps is not None else None,
        require_confirmation=bool(confirm) if confirm is not None else None,
    )


def default_profile(config: "AgentConfig") -> AgentProfile:
    """The built-in ``default`` agent: the whole skill library, all core tools.

    It is virtual (no agent.json on disk), so "run an agent" is the single model
    and the library is just the agent you get when you don't pick another one.
    """

    return AgentProfile(
        name=DEFAULT_AGENT_NAME,
        dir=config.agents_dir / DEFAULT_AGENT_NAME,
        library_dir=config.skill_library_dir,
        description="All library skills and tools (built-in).",
        builtin=True,
    )


def resolve_active(config: "AgentConfig", requested: str | None) -> AgentProfile:
    """Resolve the agent to run. Always returns a profile (never ``None``).

    Empty/omitted ``requested`` (falling back to ``config.active_agent``) or a
    reserved name yields the built-in ``default`` agent. A named-but-missing
    agent raises ``FileNotFoundError`` so the caller can report it clearly.
    """

    name = requested or config.active_agent
    if not name or name.strip().lower() in RESERVED_NAMES:
        return default_profile(config)
    return load_profile(config.agents_dir, name, config.skill_library_dir)
