"""Running a skill's script as a tool.

The contract is deliberately thin so a skill folder stays copy-portable: the
script is handed one JSON object on stdin and answers with one JSON object on
stdout. Both sides are pinned to UTF-8 — a skill that prints Japanese (or any
non-ASCII) must not depend on the machine's console codepage.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from little_agent.tools.base import ToolContext, ToolResult


@dataclass(frozen=True, slots=True)
class ScriptSkillTool:
    name: str
    description: str
    parameters: dict[str, Any]
    script_path: Path
    requires_confirmation: bool = False
    timeout_seconds: int = 30

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        payload = {
            "tool": self.name,
            "workspace": str(context.workspace),
            "readable_paths": [str(path) for path in context.readable_paths],
            "writable_paths": [str(path) for path in context.writable_paths],
            "arguments": kwargs,
        }
        # PYTHONIOENCODING pins the child's own stdout/stderr; ``encoding`` pins
        # this side's decoding. Without both, a non-ASCII result dies on a
        # console codepage that cannot represent it (cp932, cp1252, ...).
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        completed = subprocess.run(
            [sys.executable, str(self.script_path), self.name],
            cwd=self.script_path.parent.parent,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            return ToolResult(False, stderr or f"Script exited with code {completed.returncode}")

        raw = completed.stdout.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ToolResult(False, f"Script returned invalid JSON: {raw[:500]}")

        if not isinstance(data, dict):
            return ToolResult(False, "Script returned a non-object JSON response.")
        raw_images = data.get("images") or []
        images = tuple(str(item) for item in raw_images) if isinstance(raw_images, list) else ()
        return ToolResult(
            ok=bool(data.get("ok")),
            content=str(data.get("content", "")),
            images=images,
        )
