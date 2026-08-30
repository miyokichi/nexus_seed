from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from little_agent.skills.models import Skill
from little_agent.skills.script_tool import ScriptSkillTool


class SkillLoader:
    """Loads skills from one or more directories of skill folders.

    ``roots`` are directories that each contain ``<skill>/SKILL.md`` folders,
    searched in order; the first root holding a given skill folder wins, so an
    agent's own copy shadows the shared library. ``names`` optionally restricts
    loading to those skill folder names (the agent profile's capability list).
    """

    def __init__(self, roots: Path | Sequence[Path], names: Iterable[str] | None = None) -> None:
        self.roots = [roots] if isinstance(roots, Path) else list(roots)
        self.names = set(names) if names is not None else None

    def load_all(self) -> list[Skill]:
        return [self._load(path) for path in self._skill_files("SKILL.md")]

    def warning(self) -> str | None:
        """Why this loader found nothing, or ``None`` when it found skills."""

        return library_warning(self.roots, self.load_all(), self.names)

    def select_for_text(self, text: str, limit: int = 3) -> list[Skill]:
        skills = self.load_all()
        scored = [(self._score(skill, text), skill) for skill in skills]
        selected = [skill for score, skill in sorted(scored, key=lambda item: item[0], reverse=True) if score > 0]
        return selected[:limit]

    def load_tools(self) -> list[ScriptSkillTool]:
        tools: list[ScriptSkillTool] = []
        for manifest_path in self._skill_files("tools.json"):
            try:
                tools.extend(self._load_tools_manifest(manifest_path))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                print(f"Warning: skipped invalid skill tool manifest {manifest_path}: {exc}")
        return tools

    def _skill_files(self, filename: str) -> list[Path]:
        """Matching files across the roots, one per skill folder name."""

        found: dict[str, Path] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.glob(f"*/{filename}")):
                skill_name = path.parent.name
                if self.names is not None and skill_name not in self.names:
                    continue
                found.setdefault(skill_name, path)
        return [found[name] for name in sorted(found)]

    def _load(self, path: Path) -> Skill:
        raw = path.read_text(encoding="utf-8")
        name = self._title(raw) or path.parent.name
        description = self._section(raw, "Description")
        when_to_use = self._section(raw, "When to use")
        instructions = self._section(raw, "Instructions")
        allowed_tools = [
            line.strip("- ").strip()
            for line in self._section(raw, "Allowed tools").splitlines()
            if line.strip().startswith("-")
        ]
        return Skill(
            name=name,
            description=description,
            when_to_use=when_to_use,
            allowed_tools=allowed_tools,
            instructions=instructions,
            path=path,
        )

    def _load_tools_manifest(self, path: Path) -> list[ScriptSkillTool]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Skill tool manifest must be a JSON object: {path}")

        skill_dir = path.parent
        tools: list[ScriptSkillTool] = []
        manifest_tools = raw.get("tools", [])
        if not isinstance(manifest_tools, list):
            raise ValueError("'tools' must be a list.")
        for item in manifest_tools:
            if not isinstance(item, dict):
                continue
            parameters = item.get("parameters") or {}
            if not isinstance(parameters, dict):
                raise ValueError(f"Tool '{item.get('name', '(unknown)')}' parameters must be a JSON Schema object.")
            script_path = (skill_dir / str(item["script"])).resolve()
            if skill_dir.resolve() not in [script_path, *script_path.parents]:
                raise ValueError(f"Skill script is outside skill directory: {script_path}")
            if not script_path.exists():
                raise ValueError(f"Skill script does not exist: {script_path}")
            tools.append(
                ScriptSkillTool(
                    name=str(item["name"]),
                    description=str(item["description"]),
                    parameters=parameters,
                    requires_confirmation=bool(item.get("requires_confirmation", False)),
                    script_path=script_path,
                    timeout_seconds=int(item.get("timeout_seconds", 30)),
                )
            )
        return tools

    @staticmethod
    def _title(raw: str) -> str:
        match = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _section(raw: str, heading: str) -> str:
        pattern = rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
        match = re.search(pattern, raw, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _score(skill: Skill, text: str) -> int:
        haystack = f"{skill.name} {skill.description} {skill.when_to_use}".lower()
        words = {word for word in re.findall(r"[\w一-龯ぁ-んァ-ンー]+", text.lower()) if len(word) >= 2}
        score = sum(1 for word in words if word in haystack)
        if skill.name.lower() in text.lower():
            score += 3
        return score


def library_warning(roots: list[Path], skills: list[Skill], names: set[str] | None) -> str | None:
    """A warning for a library that produced no skills, or ``None`` when fine.

    An agent with no skills still runs — it just has none of the knowledge the
    library carries — so this is a warning rather than a failure. A profile that
    deliberately declares ``"skills": []`` is not warned about: it asked for
    nothing and got nothing.
    """

    if skills or names == set():
        return None
    searched = ", ".join(str(root) for root in roots) or "(nowhere)"
    missing = [str(root) for root in roots if not root.is_dir()]
    lines = [f"Warning: no skills were loaded. Searched: {searched}"]
    if missing:
        lines.append(f"  These directories do not exist: {', '.join(missing)}")
    if names:
        lines.append(f"  The agent profile asked for: {', '.join(sorted(names))}")
    lines.append(
        "  Skills are resolved as LITTLE_AGENT_SKILL_LIBRARY_DIR, then "
        "<workspace>/skills, then the built-in library."
    )
    return "\n".join(lines)
