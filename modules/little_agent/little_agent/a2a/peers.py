"""Resolving A2A peers for delegation.

A peer is either:

- **remote** — a URL from ``LITTLE_AGENT_A2A_PEERS`` (``name=url,name2=url2``) or
  passed directly as ``agent_url``. Any A2A-compliant agent works.
- **local** — the name of an ``agents/`` profile. The first delegation to it
  starts ``little_agent.a2a.serve`` on a free loopback port and reuses that
  process for the rest of the session, so the hand-off still goes over the real
  protocol without the user running a server by hand.
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import little_agent
from little_agent import agents
from little_agent.a2a.client import A2AClient, A2AClientError, fetch_agent_card
from little_agent.config import AgentConfig

SPAWN_TIMEOUT = 30.0
SPAWN_POLL_INTERVAL = 0.25


def parse_peers(raw: str | None) -> dict[str, str]:
    """Parse ``name=url,name2=url2`` into a mapping (bare URLs get a host-derived name)."""

    peers: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        name, sep, url = entry.partition("=")
        if sep:
            name, url = name.strip(), url.strip()
        else:
            url = entry
            name = url.split("//", 1)[-1].strip("/").replace(":", "-")
        if name and url:
            peers[name] = url
    return peers


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PeerPool:
    """Resolves peer names to connected A2A clients, spawning local servers on demand."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._remote = parse_peers(os.getenv("LITTLE_AGENT_A2A_PEERS"))
        self._token = os.getenv("LITTLE_AGENT_A2A_TOKEN") or None
        self._lock = threading.Lock()
        self._name_locks: dict[str, threading.Lock] = {}
        self._local: dict[str, tuple[subprocess.Popen[bytes], str]] = {}
        atexit.register(self.shutdown)

    def available(self) -> list[str]:
        """Peer names that can be delegated to: remote peers plus local profiles."""

        names = set(self._remote)
        names.update(agents.list_agents(self._config.agents_dir))
        names.add(agents.DEFAULT_AGENT_NAME)
        return sorted(names)

    def remote_names(self) -> list[str]:
        return sorted(self._remote)

    def connect(self, name: str | None = None, url: str | None = None) -> A2AClient:
        """Connect to a peer by explicit URL, remote peer name, or local profile name."""

        if url:
            return A2AClient.connect(url, token=self._token)
        key = (name or agents.DEFAULT_AGENT_NAME).strip()
        if key in self._remote:
            return A2AClient.connect(self._remote[key], token=self._token)
        return A2AClient.connect(self._ensure_local(key), token=self._token)

    def _name_lock(self, name: str) -> threading.Lock:
        with self._lock:
            lock = self._name_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._name_locks[name] = lock
            return lock

    def _ensure_local(self, name: str) -> str:
        # Fail fast with a clear error before spending a process on a bad name.
        agents.resolve_active(self._config, name)

        # Serialize per peer name: parallel delegations to the same profile must
        # start exactly one server, and later arrivals must wait for it to be
        # ready rather than racing ahead to a URL that is not listening yet.
        with self._name_lock(name):
            with self._lock:
                existing = self._local.get(name)
                if existing is not None:
                    process, base_url = existing
                    if process.poll() is None:
                        return base_url
                    self._local.pop(name, None)

            port = _free_port()
            base_url = f"http://127.0.0.1:{port}/"
            command = [
                sys.executable,
                "-m",
                "little_agent.a2a.serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]
            if name != agents.DEFAULT_AGENT_NAME:
                command += ["--agent", name]
            if _env_flag("LITTLE_AGENT_A2A_AUTO_APPROVE"):
                command.append("--auto-approve")
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(self._config.workspace),
                env=self._child_env(),
            )
            with self._lock:
                self._local[name] = (process, base_url)

            self._wait_ready(name, process, base_url)
            return base_url

    def _child_env(self) -> dict[str, str]:
        """Environment for a spawned server.

        The child must resolve the *same* workspace, agents and library as this
        process regardless of its working directory or any ``.env`` on disk, and
        must be able to import ``little_agent`` even when the package is only on
        this process's ``sys.path`` (a source checkout without an install).
        """

        env = os.environ.copy()
        env["LITTLE_AGENT_WORKSPACE"] = str(self._config.workspace)
        env["LITTLE_AGENT_READABLE_PATHS"] = _join_paths(self._config.readable_paths)
        env["LITTLE_AGENT_WRITABLE_PATHS"] = _join_paths(self._config.writable_paths)
        env["LITTLE_AGENT_AGENTS_DIR"] = str(self._config.agents_dir)
        env["LITTLE_AGENT_SKILL_LIBRARY_DIR"] = str(self._config.skill_library_dir)
        package_root = Path(little_agent.__file__).resolve().parent.parent
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{package_root}{os.pathsep}{existing}" if existing else str(package_root)
        )
        return env

    def _wait_ready(self, name: str, process: "subprocess.Popen[bytes]", base_url: str) -> None:
        deadline = time.monotonic() + SPAWN_TIMEOUT
        last_error: Any = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = b""
                if process.stderr is not None:
                    stderr = process.stderr.read() or b""
                with self._lock:
                    self._local.pop(name, None)
                detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
                raise A2AClientError(
                    f"Local A2A server for '{name}' exited immediately. {detail}".strip()
                )
            try:
                fetch_agent_card(base_url, self._token, timeout=2.0)
                return
            except A2AClientError as exc:
                last_error = exc
            time.sleep(SPAWN_POLL_INTERVAL)
        self._terminate(name)
        raise A2AClientError(
            f"Local A2A server for '{name}' did not become ready within "
            f"{SPAWN_TIMEOUT:.0f}s: {last_error}"
        )

    def _terminate(self, name: str) -> None:
        with self._lock:
            entry = self._local.pop(name, None)
        if entry is None:
            return
        process, _ = entry
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            # Release the stderr pipe so long sessions don't leak descriptors.
            if process.stderr is not None:
                process.stderr.close()

    def shutdown(self) -> None:
        """Stop every local server this pool started."""

        for name in list(self._local):
            self._terminate(name)


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _join_paths(paths: tuple[Path, ...]) -> str:
    return os.pathsep.join(str(path) for path in paths)


_shared_lock = threading.Lock()
_shared: PeerPool | None = None


def shared_pool(config: AgentConfig) -> PeerPool:
    """The session-wide pool, so switching agents reuses already-started servers."""

    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PeerPool(config)
        return _shared


def shutdown_shared() -> None:
    """Stop the session-wide pool's local servers (called when the CLI exits)."""

    global _shared
    with _shared_lock:
        pool, _shared = _shared, None
    if pool is not None:
        pool.shutdown()
