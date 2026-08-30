"""Launch modes: what each entry point actually builds.

    little-agent chat                 conversation + persistent memory
    little-agent chat --no-memory     conversation, nothing persisted
    little-agent serve-a2a ...        no conversation, no memory

These check the wiring rather than the loops: which store each mode picks, and
that the arguments a user is told to type actually parse.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.config import builtin_skills_dir
from little_agent.a2a import serve as a2a_serve
from little_agent.cli import build_memory_store, build_parser
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.factory import build_agent
from little_agent.memory.store import FileMemoryStore, NullMemoryStore

LIBRARY = builtin_skills_dir()


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
        skill_library_dir=LIBRARY,
        agents_dir=(workspace / "agents").resolve(),
        global_memory_path=(workspace / "global-memory.md").resolve(),
        global_profile_path=(workspace / "global-profile.md").resolve(),
    )


class ArgumentTests(unittest.TestCase):
    def test_bare_invocation_is_chat(self) -> None:
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)
        self.assertFalse(args.no_memory)

    def test_chat_subcommand(self) -> None:
        args = build_parser().parse_args(["chat"])
        self.assertEqual(args.command, "chat")
        self.assertFalse(args.no_memory)

    def test_chat_no_memory(self) -> None:
        args = build_parser().parse_args(["chat", "--no-memory"])
        self.assertEqual(args.command, "chat")
        self.assertTrue(args.no_memory)

    def test_bare_invocation_accepts_the_chat_options(self) -> None:
        args = build_parser().parse_args(["--no-memory", "--agent", "office"])
        self.assertIsNone(args.command)
        self.assertTrue(args.no_memory)
        self.assertEqual(args.agent, "office")

    def test_serve_a2a_subcommand(self) -> None:
        args = build_parser().parse_args(
            ["serve-a2a", "--agent", "office", "--port", "8801"]
        )
        self.assertEqual(args.command, "serve-a2a")
        self.assertEqual(args.agent, "office")
        self.assertEqual(args.port, 8801)
        self.assertEqual(args.host, "127.0.0.1")

    def test_serve_a2a_path_and_approval_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "serve-a2a",
                "--auto-approve",
                "--allow-any-path",
                "--writable-path",
                "D:/shared",
                "--writable-path",
                "D:/projects",
                "--readable-path",
                "D:/reference",
            ]
        )
        self.assertTrue(args.auto_approve)
        self.assertTrue(args.allow_any_path)
        self.assertEqual(args.writable_path, ["D:/shared", "D:/projects"])
        self.assertEqual(args.readable_path, ["D:/reference"])

    def test_serve_a2a_module_entry_point_takes_the_same_options(self) -> None:
        parser = a2a_serve.add_arguments(__import__("argparse").ArgumentParser())
        args = parser.parse_args(["--port", "8801", "--allow-any-path"])
        self.assertEqual(args.port, 8801)
        self.assertTrue(args.allow_any_path)


class MemoryStoreSelectionTests(unittest.TestCase):
    def test_chat_with_memory_gets_the_file_store(self) -> None:
        with TemporaryDirectory() as tmp:
            store = build_memory_store(_config(Path(tmp)), enabled=True)
            self.assertIsInstance(store, FileMemoryStore)
            self.assertTrue(store.enabled)

    def test_chat_without_memory_gets_the_null_store(self) -> None:
        with TemporaryDirectory() as tmp:
            store = build_memory_store(_config(Path(tmp)), enabled=False)
            self.assertIsInstance(store, NullMemoryStore)
            self.assertFalse(store.enabled)

    def test_the_file_store_points_at_the_configured_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            store = build_memory_store(_config(root), enabled=True)
            self.assertEqual(store.workspace_memory.path, root / "memory.md")
            self.assertEqual(store.workspace_profile.path, root / "profile.md")
            self.assertEqual(store.global_memory.path, root / "global-memory.md")
            self.assertEqual(store.global_profile.path, root / "global-profile.md")


class BuildAgentMemoryTests(unittest.TestCase):
    def test_an_agent_built_without_a_store_remembers_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x")
            )
            self.assertIsInstance(agent.memory, NullMemoryStore)

    def test_a_store_passed_in_reaches_the_agent(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            store = FileMemoryStore.from_config(config)
            agent = build_agent(
                config,
                agents.default_profile(config),
                lambda *_: True,
                StopController("x"),
                memory=store,
            )
            self.assertIs(agent.memory, store)
            self.assertIn("update_workspace_memory", agent.tools.names())


class ServePathOptionTests(unittest.TestCase):
    def test_relative_paths_resolve_against_the_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            merged = a2a_serve.with_cli_paths(_config(root), ["ref"], ["out"])
            self.assertEqual(merged.readable_paths, (root / "ref",))
            self.assertEqual(merged.writable_paths, (root / "out",))

    def test_no_paths_leaves_the_config_alone(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            self.assertIs(a2a_serve.with_cli_paths(config, [], []), config)


if __name__ == "__main__":
    unittest.main()
