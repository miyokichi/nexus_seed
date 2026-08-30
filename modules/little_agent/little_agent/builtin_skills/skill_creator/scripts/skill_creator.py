from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "create_skill":
            result = create_skill(workspace, arguments)
        elif tool == "validate_skill":
            result = validate_skill(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown skill creator tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Skill creator script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def create_skill(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = normalize_name(str(arguments.get("name") or ""))
    description = str(arguments.get("description") or "").strip()
    overwrite = bool(arguments.get("overwrite", False))
    if not name:
        return {"ok": False, "content": "Skill name is required."}
    if not description:
        return {"ok": False, "content": "Skill description is required."}

    skill_dir = safe_skill_dir(workspace, name)
    if skill_dir.exists() and not overwrite:
        return {"ok": False, "content": f"Skill already exists: skills/{name}"}

    skill_dir.mkdir(parents=True, exist_ok=True)
    write_file(skill_dir / "SKILL.md", skill_markdown(name, description), overwrite)

    created = [f"skills/{name}/SKILL.md"]
    include_scripts = bool(arguments.get("include_scripts", False))
    include_tools_manifest = bool(arguments.get("include_tools_manifest", False))

    if include_scripts:
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        write_file(scripts_dir / "example_tool.py", example_script(), overwrite)
        created.append(f"skills/{name}/scripts/example_tool.py")

    if bool(arguments.get("include_references", False)):
        (skill_dir / "references").mkdir(exist_ok=True)
        created.append(f"skills/{name}/references/")

    if bool(arguments.get("include_assets", False)):
        (skill_dir / "assets").mkdir(exist_ok=True)
        created.append(f"skills/{name}/assets/")

    if include_tools_manifest:
        if not include_scripts:
            (skill_dir / "scripts").mkdir(exist_ok=True)
            write_file(skill_dir / "scripts" / "example_tool.py", example_script(), overwrite)
            created.append(f"skills/{name}/scripts/example_tool.py")
        write_file(skill_dir / "tools.json", tools_manifest(name), overwrite)
        created.append(f"skills/{name}/tools.json")

    return {"ok": True, "content": "Created:\n" + "\n".join(created)}


def validate_skill(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = normalize_name(str(arguments.get("name") or ""))
    if not name:
        return {"ok": False, "content": "Skill name is required."}
    skill_dir = safe_skill_dir(workspace, name)
    issues: list[str] = []

    skill_md = skill_dir / "SKILL.md"
    if not skill_dir.exists():
        issues.append(f"Missing directory: skills/{name}")
    if not skill_md.exists():
        issues.append(f"Missing file: skills/{name}/SKILL.md")
    else:
        text = skill_md.read_text(encoding="utf-8")
        for heading in ["# ", "## Description", "## When to use", "## Allowed tools", "## Instructions"]:
            if heading not in text:
                issues.append(f"Missing heading in SKILL.md: {heading.strip()}")

    manifest = skill_dir / "tools.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            tools = data.get("tools")
            if not isinstance(tools, list):
                issues.append("tools.json must contain a tools list.")
            else:
                for item in tools:
                    validate_tool_manifest_item(skill_dir, item, issues)
        except json.JSONDecodeError as exc:
            issues.append(f"tools.json is invalid JSON: {exc}")

    if issues:
        return {"ok": False, "content": "Validation failed:\n" + "\n".join(f"- {issue}" for issue in issues)}
    return {"ok": True, "content": f"Skill is valid: skills/{name}"}


def validate_tool_manifest_item(skill_dir: Path, item: Any, issues: list[str]) -> None:
    if not isinstance(item, dict):
        issues.append("Each tools.json item must be an object.")
        return
    for key in ["name", "description", "script", "parameters"]:
        if key not in item:
            issues.append(f"Tool item is missing key: {key}")
    script = skill_dir / str(item.get("script", ""))
    if item.get("script") and not script.exists():
        issues.append(f"Tool script does not exist: {script.relative_to(skill_dir)}")


def safe_skill_dir(workspace: Path, name: str) -> Path:
    skills_root = (workspace / "skills").resolve()
    skill_dir = (skills_root / name).resolve()
    if skills_root not in [skill_dir, *skill_dir.parents]:
        raise ValueError("Skill path escaped the workspace.")
    return skill_dir


def normalize_name(name: str) -> str:
    lowered = name.strip().lower().replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    return normalized[:63]


def write_file(path: Path, content: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.write_text(content, encoding="utf-8")


def skill_markdown(name: str, description: str) -> str:
    return f"""# {name}

## Description
{description}

## When to use
- TODO: このSkillを使うべきユーザー依頼や状況を書く。

## Allowed tools
- TODO: 使用してよいTool名を書く。Toolがない場合は `none` と書く。

## Instructions
- TODO: Agentが従う手順、注意点、判断基準を書く。
- 必要な詳細資料は `references/` に置き、ここから参照する。
- 実行ロジックが必要な場合は `scripts/` と `tools.json` を使う。
"""


def tools_manifest(skill_name: str) -> str:
    tool_name = normalize_name(skill_name).replace("-", "_") + "_example"
    data = {
        "tools": [
            {
                "name": tool_name,
                "description": f"Example script-backed tool for {skill_name}.",
                "script": "scripts/example_tool.py",
                "requires_confirmation": False,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Input text."}
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def example_script() -> str:
    return '''from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    arguments = payload.get("arguments") or {}
    text = str(arguments.get("text") or "")
    print(json.dumps({"ok": True, "content": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


if __name__ == "__main__":
    raise SystemExit(main())
