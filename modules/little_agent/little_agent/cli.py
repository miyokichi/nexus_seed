"""The ``little-agent`` command line: two ways into one runtime.

    little-agent chat                       interactive chat, persistent memory on
    little-agent chat --no-memory           interactive chat, nothing persisted
    little-agent serve-a2a --agent x --port 8801    serve the agent over A2A

Both modes build the same :class:`~little_agent.agent.Agent` through
:func:`~little_agent.factory.build_agent`. They differ in two deliberate ways:
chat wraps the agent in a :class:`~little_agent.session.ChatSession` so turns
accumulate, and chat is the only mode that may be handed a persistent
:class:`~little_agent.memory.store.MemoryStore`.

Running with no subcommand starts ``chat``, which is what the bare command has
always done.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from little_agent import agents
from little_agent.a2a import serve as a2a_serve
from little_agent.commands import CommandContext, CommandRegistry
from little_agent.config import AgentConfig, describe_skill_library
from little_agent.control import StopController
from little_agent.factory import build_agent
from little_agent.memory.store import FileMemoryStore, MemoryStore, NullMemoryStore
from little_agent.session import ChatSession


def _make_confirm(stop_hotkey: str):
    def _confirm(tool_name: str, arguments: dict[str, Any]) -> bool:
        print(f"\nTool confirmation required: {tool_name}")
        print(json.dumps(arguments, ensure_ascii=False, indent=2))
        print(
            "Approving runs this tool AND auto-approves the rest of this session. "
            f"Emergency stop: {stop_hotkey} (or move the mouse to a screen corner)."
        )
        answer = input("Approve for this session? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    return _confirm


def _select_profile_at_launch(config: AgentConfig, requested: str | None) -> agents.AgentProfile:
    """Pick the launch agent: explicit request > env default > picker > default."""

    name = requested or config.active_agent
    if name:
        try:
            return agents.resolve_active(config, name)
        except FileNotFoundError:
            available = agents.list_agents(config.agents_dir)
            hint = ", ".join(available) if available else "(none)"
            print(f"Agent '{name}' not found. Available: {hint}")

    available = agents.list_agents(config.agents_dir)
    if not available:
        return agents.default_profile(config)

    print("Available agents:")
    print(f"  0) {agents.DEFAULT_AGENT_NAME} (all library skills & tools)")
    for index, agent_name in enumerate(available, start=1):
        print(f"  {index}) {agent_name}")
    try:
        choice = input("Select an agent [0]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return agents.default_profile(config)
    if not choice or choice == "0":
        return agents.default_profile(config)
    if choice.isdigit() and 1 <= int(choice) <= len(available):
        return agents.load_profile(
            config.agents_dir, available[int(choice) - 1], config.skill_library_dir
        )
    if choice in available:
        return agents.load_profile(config.agents_dir, choice, config.skill_library_dir)
    print(f"Unknown selection '{choice}', using '{agents.DEFAULT_AGENT_NAME}'.")
    return agents.default_profile(config)


def build_memory_store(config: AgentConfig, enabled: bool) -> MemoryStore:
    """The store a chat session runs with.

    ``--no-memory`` gives a null store, which is not "memory that declines to
    write" — the files are never opened and the memory tools do not exist.
    """

    return FileMemoryStore.from_config(config) if enabled else NullMemoryStore()


def chat(args: argparse.Namespace) -> None:
    """Run the interactive chat loop until the user leaves."""

    config = AgentConfig.from_env()
    config = a2a_serve.with_cli_paths(config, args.readable_path, args.writable_path)
    config.workspace.mkdir(parents=True, exist_ok=True)
    stop = StopController(config.stop_hotkey)
    confirm = _make_confirm(config.stop_hotkey)
    memory = build_memory_store(config, enabled=not args.no_memory)

    profile = _select_profile_at_launch(config, args.agent)
    agent = build_agent(config, profile, confirm, stop, memory=memory)
    session = ChatSession(agent)
    registry = CommandRegistry(config.commands_dir, config.global_commands_dir)
    ctx = CommandContext(session=session, registry=registry, active_agent=profile.name)

    def activate(name: str) -> str:
        switched = agents.resolve_active(config, name)
        # The same store: switching agent changes capability, not what is
        # remembered, and the transcript carries on.
        session.switch_agent(build_agent(config, switched, confirm, stop, memory=memory))
        ctx.active_agent = switched.name
        if switched.builtin:
            return f"Switched to '{switched.name}' (all library skills & tools)."
        return f"Switched to agent '{switched.name}' ({len(switched.enabled_skills())} skill(s))."

    ctx.activate = activate

    mode = "OpenAI" if config.openai_api_key else "local fallback"
    print(f"Little Agent ({mode})")
    print(f"Workspace: {config.workspace}")
    print(f"Skills: {describe_skill_library(config)}")
    print(f"Active agent: {ctx.active_agent}")
    print(f"Memory: {memory.describe()}")
    print(f"Emergency stop while the agent acts: {config.stop_hotkey}")
    warning = agent.skills.warning()
    if warning:
        print(warning)
    print("Type /help for commands, /exit to quit.\n")

    _repl(ctx)

    if memory.enabled:
        try:
            if session.learn():
                print("[memory] learned from this session.")
        except Exception as exc:  # noqa: BLE001 - never let learning break the exit.
            print(f"[memory] auto-learning skipped: {exc}")
    _shutdown()


def _repl(ctx: CommandContext) -> None:
    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_text:
            continue

        result = ctx.registry.dispatch(ctx, user_text)
        if result is None:
            print(ctx.session.send(user_text))
            print()
            continue
        if result.output is not None:
            print(result.output)
        if result.agent_prompt is not None:
            print(ctx.session.send(result.agent_prompt))
        if result.should_exit:
            return
        print()


def _add_chat_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--agent", help="Name of the agent profile to run (under agents/).")
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help=(
            "Do not load or save persistent memory. The conversation is still kept "
            "for the length of the session."
        ),
    )
    parser.add_argument(
        "--readable-path",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional file or directory path that tools may read. Repeatable.",
    )
    parser.add_argument(
        "--writable-path",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional file or directory path that tools may read and write. Repeatable.",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="little-agent",
        description="Little Agent: chat with the agent, or serve it over A2A.",
    )
    subcommands = parser.add_subparsers(dest="command")
    _add_chat_arguments(
        subcommands.add_parser("chat", help="Interactive chat (the default with no subcommand).")
    )
    a2a_serve.add_arguments(
        subcommands.add_parser("serve-a2a", help="Serve this agent over the A2A protocol.")
    )
    # The bare command is chat, so its options must parse without the subcommand.
    _add_chat_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "serve-a2a":
        a2a_serve.run(args)
        return
    chat(args)


def _shutdown() -> None:
    try:
        from little_agent.a2a.peers import shutdown_shared

        shutdown_shared()  # stop any local A2A servers started for delegation
    except Exception as exc:  # noqa: BLE001 - never let cleanup break exit.
        print(f"[a2a] peer shutdown skipped: {exc}")


if __name__ == "__main__":
    main()
