from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_MAX_OUTPUT_BYTES = 50_000


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "git_status":
            result = git_status(workspace)
        elif tool == "git_diff":
            result = git_diff(workspace, arguments)
        elif tool == "git_log":
            result = git_log(workspace, arguments)
        elif tool == "git_add":
            result = git_add(workspace, arguments)
        elif tool == "git_commit":
            result = git_commit(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown git tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Git script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def run_git(workspace: Path, args: list[str]) -> tuple[bool, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = completed.stdout[:_MAX_OUTPUT_BYTES]
    if completed.returncode != 0:
        error = completed.stderr.strip()
        return False, error or f"git exited with code {completed.returncode}"
    if len(completed.stdout) > _MAX_OUTPUT_BYTES:
        output += f"\n... (truncated at {_MAX_OUTPUT_BYTES} bytes)"
    return True, output.strip()


def git_status(workspace: Path) -> dict[str, Any]:
    ok, output = run_git(workspace, ["status", "--short", "--branch"])
    return {"ok": ok, "content": output or "(nothing to report)"}


def git_diff(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    staged = bool(arguments.get("staged", False))
    path = str(arguments.get("path") or "").strip()

    cmd = ["diff"]
    if staged:
        cmd.append("--cached")
    if path:
        safe_path = _safe_relative_path(workspace, path)
        if safe_path is None:
            return {"ok": False, "content": f"Path is outside workspace: {path}"}
        cmd += ["--", safe_path]

    ok, output = run_git(workspace, cmd)
    return {"ok": ok, "content": output or "(no differences)"}


def git_log(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(arguments.get("limit") or 10), 100))
    ok, output = run_git(workspace, ["log", f"--max-count={limit}", "--oneline", "--decorate"])
    return {"ok": ok, "content": output or "(no commits)"}


def git_add(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = str(arguments.get("path") or "").strip()
    if not path:
        return {"ok": False, "content": "path is required."}
    if path != ".":
        safe_path = _safe_relative_path(workspace, path)
        if safe_path is None:
            return {"ok": False, "content": f"Path is outside workspace: {path}"}
        path = safe_path
    ok, output = run_git(workspace, ["add", "--", path])
    if ok:
        _, status = run_git(workspace, ["status", "--short"])
        return {"ok": True, "content": f"Staged.\n{status}"}
    return {"ok": ok, "content": output}


def git_commit(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    message = str(arguments.get("message") or "").strip()
    if not message:
        return {"ok": False, "content": "Commit message is required."}
    ok, output = run_git(workspace, ["commit", "-m", message])
    return {"ok": ok, "content": output}


def _safe_relative_path(workspace: Path, raw: str) -> str | None:
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve()
    except Exception:
        return None
    if workspace not in [resolved, *resolved.parents]:
        return None
    return str(resolved.relative_to(workspace))


if __name__ == "__main__":
    raise SystemExit(main())
