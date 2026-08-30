"""Script-backed tools for creating and managing agent profiles.

Runs as a standalone subprocess (the Little Agent ScriptSkillTool protocol), so
it cannot import ``little_agent.agents``; the small slug/copy helpers are
duplicated here on purpose, matching how other skill scripts are self-contained.

Agents live under ``<workspace>/agents/<name>/`` with an ``agent.json`` profile
and a ``skills/`` directory whose folders are COPIED from the skill library at
``<workspace>/skills/``. On-disk changes take effect on the next launch or after
a ``/agent`` switch in the running CLI.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

AGENT_CONFIG_FILE = "agent.json"

# Reserved for the built-in "default" agent (the whole library); not creatable.
RESERVED_NAMES = {"default", "library", "none", "-"}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        agents_dir = workspace / "agents"
        library_dir = workspace / "skills"

        handlers = {
            "create_agent": create_agent,
            "list_agents": list_agents,
            "show_agent": show_agent,
            "add_agent_skill": add_agent_skill,
            "remove_agent_skill": remove_agent_skill,
            "set_agent_core_tools": set_agent_core_tools,
            "delete_agent": delete_agent,
        }
        handler = handlers.get(tool)
        if handler is None:
            result = {"ok": False, "content": f"Unknown agent_manager tool: {tool}"}
        else:
            result = handler(agents_dir, library_dir, arguments)
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"agent_manager script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


# --- helpers ---------------------------------------------------------------


def normalize_name(name: str) -> str:
    lowered = name.strip().lower().replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    return normalized[:63]


def safe_child(root: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"Invalid name: {name!r}")
    root = root.resolve()
    child = (root / name).resolve()
    if root not in [child, *child.parents]:
        raise ValueError(f"Path escaped the parent directory: {name!r}")
    return child


def library_skills(library_dir: Path) -> list[str]:
    if not library_dir.exists():
        return []
    return sorted(
        path.name
        for path in library_dir.iterdir()
        if path.is_dir() and (path / "SKILL.md").exists()
    )


def enabled_skills(agent_path: Path) -> list[str]:
    skills_dir = agent_path / "skills"
    if not skills_dir.exists():
        return []
    return sorted(
        path.name
        for path in skills_dir.iterdir()
        if path.is_dir() and (path / "SKILL.md").exists()
    )


def copy_skill(library_dir: Path, dest_skills_dir: Path, skill: str) -> None:
    src = safe_child(library_dir, skill)
    if not (src / "SKILL.md").exists():
        available = ", ".join(library_skills(library_dir)) or "(none)"
        raise ValueError(f"Skill not found in library: {skill}. Available: {available}")
    dst = safe_child(dest_skills_dir, skill)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def read_profile(agents_dir: Path, name: str) -> tuple[Path, dict[str, Any]]:
    agent_path = safe_child(agents_dir, normalize_name(name))
    config_path = agent_path / AGENT_CONFIG_FILE
    if not config_path.exists():
        raise FileNotFoundError(f"No such agent: {normalize_name(name)}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{AGENT_CONFIG_FILE} must contain a JSON object.")
    return agent_path, data


def write_profile(agent_path: Path, data: dict[str, Any]) -> None:
    agent_path.mkdir(parents=True, exist_ok=True)
    (agent_path / AGENT_CONFIG_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def as_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [item.strip() for item in re.split(r"[,\s]+", value) if item.strip()]
        return parts or None
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return parts or None
    raise ValueError("Expected a list or comma/space-separated string.")


# --- tools -----------------------------------------------------------------


def create_agent(agents_dir: Path, library_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = normalize_name(str(arguments.get("name") or ""))
    if not name:
        return {"ok": False, "content": "Agent name is required."}
    if name in RESERVED_NAMES:
        return {"ok": False, "content": f"'{name}' is reserved for the built-in library agent."}
    description = str(arguments.get("description") or "").strip()
    overwrite = bool(arguments.get("overwrite", False))
    skills = as_str_list(arguments.get("skills")) or []
    core_tools = as_str_list(arguments.get("core_tools"))

    agent_path = safe_child(agents_dir, name)
    if agent_path.exists() and not overwrite:
        return {"ok": False, "content": f"Agent already exists: {name} (use overwrite=true to replace)."}

    (agent_path / "skills").mkdir(parents=True, exist_ok=True)
    for skill in skills:
        copy_skill(library_dir, agent_path / "skills", skill)

    write_profile(
        agent_path,
        {
            "name": name,
            "description": description,
            "model": None,
            "core_tools": core_tools,
            "max_tool_steps": None,
            "require_confirmation": None,
        },
    )
    copied = ", ".join(skills) if skills else "(none)"
    tools_note = ", ".join(core_tools) if core_tools else "all core tools"
    return {
        "ok": True,
        "content": (
            f"Created agent '{name}'.\n"
            f"Skills copied: {copied}\n"
            f"Core tools: {tools_note}\n"
            f"Run it with: little-agent --agent {name} (or /agent {name})."
        ),
    }


def list_agents(agents_dir: Path, library_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    if not agents_dir.exists():
        return {"ok": True, "content": "No agents defined yet."}
    names = sorted(
        path.name
        for path in agents_dir.iterdir()
        if path.is_dir() and (path / AGENT_CONFIG_FILE).exists()
    )
    if not names:
        return {"ok": True, "content": "No agents defined yet."}
    lines = [f"Agents ({len(names)}):"]
    for name in names:
        agent_path, data = read_profile(agents_dir, name)
        description = str(data.get("description") or "(no description)")
        lines.append(f"- {name}: {len(enabled_skills(agent_path))} skill(s) - {description}")
    return {"ok": True, "content": "\n".join(lines)}


def show_agent(agents_dir: Path, library_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments.get("name") or "")
    agent_path, data = read_profile(agents_dir, name)
    skills = enabled_skills(agent_path)
    core_tools = data.get("core_tools")
    tools_note = ", ".join(core_tools) if isinstance(core_tools, list) and core_tools else "all core tools"
    lines = [
        f"Agent: {data.get('name') or agent_path.name}",
        f"Description: {data.get('description') or '(none)'}",
        f"Model: {data.get('model') or '(env default)'}",
        f"Core tools: {tools_note}",
        f"Skills ({len(skills)}): {', '.join(skills) if skills else '(none)'}",
    ]
    return {"ok": True, "content": "\n".join(lines)}


def add_agent_skill(agents_dir: Path, library_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments.get("name") or "")
    skill = str(arguments.get("skill") or "").strip()
    if not skill:
        return {"ok": False, "content": "skill is required."}
    agent_path, _ = read_profile(agents_dir, name)
    copy_skill(library_dir, agent_path / "skills", skill)
    return {"ok": True, "content": f"Added skill '{skill}' to agent '{normalize_name(name)}'."}


def remove_agent_skill(agents_dir: Path, library_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments.get("name") or "")
    skill = str(arguments.get("skill") or "").strip()
    if not skill:
        return {"ok": False, "content": "skill is required."}
    agent_path, _ = read_profile(agents_dir, name)
    target = safe_child(agent_path / "skills", skill)
    if not target.exists():
        return {"ok": False, "content": f"Agent '{normalize_name(name)}' has no skill: {skill}"}
    shutil.rmtree(target)
    return {"ok": True, "content": f"Removed skill '{skill}' from agent '{normalize_name(name)}'."}


def set_agent_core_tools(agents_dir: Path, library_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments.get("name") or "")
    core_tools = as_str_list(arguments.get("core_tools"))
    agent_path, data = read_profile(agents_dir, name)
    data["core_tools"] = core_tools
    write_profile(agent_path, data)
    tools_note = ", ".join(core_tools) if core_tools else "all core tools"
    return {"ok": True, "content": f"Agent '{normalize_name(name)}' core tools set to: {tools_note}."}


def delete_agent(agents_dir: Path, library_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = normalize_name(str(arguments.get("name") or ""))
    agent_path = safe_child(agents_dir, name)
    if not agent_path.exists():
        return {"ok": False, "content": f"No such agent: {name}"}
    shutil.rmtree(agent_path)
    return {"ok": True, "content": f"Deleted agent '{name}'."}


if __name__ == "__main__":
    raise SystemExit(main())
