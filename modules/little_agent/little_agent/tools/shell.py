from __future__ import annotations

import subprocess

from little_agent.tools.base import ToolContext, ToolResult


_BLOCKED_TOKENS = [
    "Remove-Item",
    "rm ",
    "rmdir",
    "del ",
    "Format-Volume",
    "Stop-Computer",
    "Restart-Computer",
    "Set-ExecutionPolicy",
]


class RunPowerShellTool:
    name = "run_powershell"
    description = "Run a PowerShell command in the workspace. Destructive commands are blocked by a simple guard."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "PowerShell command to run."},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        command = str(kwargs["command"])
        timeout = int(kwargs.get("timeout_seconds", 30))
        lowered = f" {command} ".lower()
        for token in _BLOCKED_TOKENS:
            if token.lower() in lowered:
                return ToolResult(False, f"Blocked potentially destructive command: {token}")

        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            cwd=context.workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = completed.stdout.strip()
        error = completed.stderr.strip()
        body = output
        if error:
            body = f"{body}\nSTDERR:\n{error}".strip()
        return ToolResult(completed.returncode == 0, body or f"Exit code: {completed.returncode}")
