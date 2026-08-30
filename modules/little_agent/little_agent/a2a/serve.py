"""Run a Little Agent profile as an A2A server.

    little-agent serve-a2a --agent office --port 8801
    python -m little_agent.a2a.serve --agent office --port 8801

Each A2A task gets a freshly built agent, a run that keeps nothing and a
:class:`~little_agent.memory.store.NullMemoryStore`, so tasks share no context
and no persistent memory is read or written on anyone's behalf. Tools that need
confirmation are refused unless auto-approval is enabled, because a served agent
has no human at a prompt.

A caller may ask to work in a particular directory (``workspace``, read+write)
and to read named paths outside it (``allowed_paths``, read only). Each is
authorized here against the matching half of this server's own configuration
before any work starts.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from little_agent import agents
from little_agent.a2a.grant import GrantPolicy, WorkGrant
from little_agent.a2a.models import agent_card, agent_skill
from little_agent.a2a.server import DEFAULT_PORT, A2AService, serve
from little_agent.agents import AgentProfile
from little_agent.config import AgentConfig, describe_skill_library
from little_agent.factory import build_agent
from little_agent.skills.loader import SkillLoader

VERSION = "0.1.0"


def _card_skills(profile: AgentProfile) -> list[dict[str, Any]]:
    """Advertise the profile's Little Agent skills as A2A skills."""

    skills = []
    loader = SkillLoader(
        [root.resolve() for root in profile.skill_roots()], names=profile.skill_names()
    )
    for skill in loader.load_all():
        skills.append(
            agent_skill(
                skill_id=skill.name,
                name=skill.name,
                description=skill.description or skill.when_to_use or skill.name,
                tags=[tag.strip() for tag in skill.allowed_tools if tag.strip()][:8],
            )
        )
    if not skills:
        skills.append(
            agent_skill(
                skill_id="general",
                name="general",
                description="General assistance using this agent's core tools.",
            )
        )
    return skills


def build_service(
    config: AgentConfig,
    profile: AgentProfile,
    host: str,
    port: int,
    token: str | None = None,
    auto_approve: bool = False,
    allow_any_path: bool = False,
) -> A2AService:
    """Build the served A2A service for a profile.

    The caller's requested workspace is authorized against this server's writable
    roots and its allowed paths against the readable ones, so a peer can only
    ever be handed access this server already has. ``allow_any_path`` lifts that
    check for a deliberately open, private server.
    """

    description = profile.description or f"Little Agent profile '{profile.name}'."

    def confirm(tool_name: str, _arguments: dict[str, Any]) -> bool:
        if auto_approve:
            return True
        print(f"[a2a] denied '{tool_name}' (needs confirmation; no human at this server).")
        return False

    def agent_factory(depth: int, stop: Any, grant: WorkGrant):
        # One fresh agent per task: its own context, its own stop controller, the
        # workspace and paths this task was granted, and no persistent memory.
        task_config = grant.apply(config)
        task_config.workspace.mkdir(parents=True, exist_ok=True)
        return build_agent(task_config, profile, confirm, stop, depth=depth)

    card = agent_card(
        name=f"little-agent/{profile.name}",
        description=description,
        url=f"http://{host}:{port}/",
        version=VERSION,
        skills=_card_skills(profile),
        requires_auth=bool(token),
    )
    return A2AService(
        card,
        agent_factory,
        token=token,
        grant_policy=GrantPolicy.from_config(config, allow_any=allow_any_path),
    )


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Define the serve-a2a options (shared with the ``little-agent`` CLI)."""

    parser.add_argument("--agent", help="Agent profile to serve (default: the whole library).")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})."
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Allow tools that normally require confirmation (no human is at this server).",
    )
    parser.add_argument(
        "--readable-path",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "File or directory this agent may read, and may grant to a task as a "
            "read-only allowed path. Repeatable."
        ),
    )
    parser.add_argument(
        "--writable-path",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "File or directory outside the workspace this agent may read and write, "
            "and may grant to a task as its workspace. Repeatable."
        ),
    )
    parser.add_argument(
        "--allow-any-path",
        action="store_true",
        help="Accept any workspace/path a caller asks for (only for a trusted, private server).",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Serve until interrupted, using the options defined by :func:`add_arguments`."""

    config = AgentConfig.from_env()
    config = with_cli_paths(config, args.readable_path, args.writable_path)
    config.workspace.mkdir(parents=True, exist_ok=True)
    profile = agents.resolve_active(config, args.agent)
    token = os.getenv("LITTLE_AGENT_A2A_TOKEN") or None
    auto_approve = args.auto_approve or _env_flag("LITTLE_AGENT_A2A_AUTO_APPROVE")
    allow_any_path = args.allow_any_path or _env_flag("LITTLE_AGENT_A2A_ALLOW_ANY_PATH")

    service = build_service(
        config,
        profile,
        args.host,
        args.port,
        token=token,
        auto_approve=auto_approve,
        allow_any_path=allow_any_path,
    )
    httpd = serve(service, args.host, args.port)
    print(f"A2A agent '{service.card['name']}' on http://{args.host}:{args.port}/")
    print(f"Agent Card: http://{args.host}:{args.port}/.well-known/agent-card.json")
    print(f"Auth: {'bearer token required' if token else 'none'}")
    print(f"Confirmation-required tools: {'auto-approved' if auto_approve else 'denied'}")
    print(f"Workspace: {config.workspace}")
    print(f"Skills: {describe_skill_library(config)}")
    warning = SkillLoader(
        [root.resolve() for root in profile.skill_roots()], names=profile.skill_names()
    ).warning()
    if warning:
        print(warning)
    if allow_any_path:
        print("Grantable as a workspace (read+write): any (unrestricted)")
        print("Grantable as an allowed path (read):   any (unrestricted)")
    else:
        writable = ", ".join(str(path) for path in config.writable_paths)
        readable = ", ".join(str(path) for path in (*config.writable_paths, *config.readable_paths))
        print(f"Grantable as a workspace (read+write): {writable or '(workspace only)'}")
        print(f"Grantable as an allowed path (read):   {readable or '(workspace only)'}")
    print("Memory: off (each task is independent; nothing is persisted)")
    print("Ctrl+C to stop.")
    # A server's stdout is usually a pipe, and a pipe is block-buffered: without
    # this the banner (and any skill-library warning above it) would sit unseen
    # in the buffer for the life of the process.
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        httpd.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = add_arguments(argparse.ArgumentParser(prog="little-agent-a2a"))
    run(parser.parse_args(argv))


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def with_cli_paths(
    config: AgentConfig,
    readable_paths: list[str],
    writable_paths: list[str],
) -> AgentConfig:
    """Merge repeatable ``--readable-path`` / ``--writable-path`` into the config."""

    if not readable_paths and not writable_paths:
        return config
    return replace(
        config,
        readable_paths=(
            *config.readable_paths,
            *(_resolve(config, path) for path in readable_paths),
        ),
        writable_paths=(
            *config.writable_paths,
            *(_resolve(config, path) for path in writable_paths),
        ),
    )


def _resolve(config: AgentConfig, raw: str) -> Path:
    """Resolve a CLI path, taking a relative one from the workspace."""

    path = Path(raw).expanduser()
    return (path if path.is_absolute() else config.workspace / path).resolve()


if __name__ == "__main__":
    main()
