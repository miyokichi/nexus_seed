"""Own one real little_agent server subprocess for an integration test.

The Agent stays behind its own process and its own A2A endpoint, which is the
whole point of these tests: NEXUS SEED must delegate to it, not stand in for
it.  Shared by every test that needs a live Agent Runtime.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LITTLE_AGENT_ROOT = REPO_ROOT / "modules" / "little_agent"
LITTLE_AGENT_PYTHON = LITTLE_AGENT_ROOT / ".venv" / "bin" / "python"

#: How long the server is given to serve its Agent Card before the test calls
#: the process broken rather than slow.
STARTUP_TIMEOUT_SECONDS = 15.0


def free_port() -> int:
    """Return a port nothing is listening on right now."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LittleAgentProcess(AbstractContextManager["LittleAgentProcess"]):
    """One little_agent server subprocess, with its bounded audit log retained."""

    def __init__(
        self,
        *,
        llm_url: str,
        workspace_root: Path,
        readable_root: Path,
        temp_root: Path,
        max_tool_steps: int = 8,
    ) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.output = ""
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "OPENAI_API_KEY": "local-e2e-key",
                "OPENAI_BASE_URL": llm_url,
                "LITTLE_AGENT_MODEL": "deterministic-e2e",
                "LITTLE_AGENT_WORKSPACE": str(workspace_root),
                "LITTLE_AGENT_READABLE_PATHS": str(readable_root),
                "LITTLE_AGENT_MAX_TOOL_STEPS": str(max_tool_steps),
                "LITTLE_AGENT_MAX_DELEGATION_DEPTH": "0",
                "LITTLE_AGENT_ENABLE_LOGGING": "false",
                "LITTLE_AGENT_LOG_DIR": str(temp_root / "little-agent-logs"),
                "LITTLE_AGENT_AGENTS_DIR": str(temp_root / "agents"),
            }
        )
        self.process = subprocess.Popen(
            [
                str(LITTLE_AGENT_PYTHON),
                "-m",
                "little_agent.a2a.serve",
                "--agent",
                "default",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--auto-approve",
                "--readable-path",
                str(readable_root),
            ],
            cwd=LITTLE_AGENT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def __enter__(self) -> "LittleAgentProcess":
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        card_url = f"{self.url}/.well-known/agent-card.json"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stop()
                raise AssertionError(
                    f"little_agent exited during startup:\n{self.output}"
                )
            try:
                with urllib.request.urlopen(card_url, timeout=0.25) as response:
                    if response.status == 200:
                        return self
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        self.stop()
        raise AssertionError(f"little_agent did not become ready:\n{self.output}")

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        """Stop the server and keep everything it printed."""

        if self.process.poll() is None:
            self.process.terminate()
        try:
            output, _ = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            output, _ = self.process.communicate(timeout=5)
        self.output += output or ""


__all__ = [
    "LITTLE_AGENT_PYTHON",
    "LITTLE_AGENT_ROOT",
    "LittleAgentProcess",
    "REPO_ROOT",
    "STARTUP_TIMEOUT_SECONDS",
    "free_port",
]
