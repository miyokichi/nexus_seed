from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent.config import builtin_skills_dir
from little_agent.agent import Agent
from little_agent.commands import (
    CommandContext,
    CommandRegistry,
    load_custom_commands,
    parse_frontmatter,
    render_template,
)
from little_agent.config import AgentConfig
from little_agent.session import ChatSession
from little_agent.skills.loader import SkillLoader


def write_profile(agents_dir: Path, name: str, **profile) -> None:
    directory = agents_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.json").write_text(
        json.dumps({"name": name, **profile}), encoding="utf-8"
    )


def _make_registry(project: Path, global_dir: Path) -> CommandRegistry:
    return CommandRegistry(project.resolve(), global_dir.resolve())


def _context(registry: CommandRegistry, workspace: Path) -> tuple[CommandContext, Agent]:
    config = AgentConfig(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
    )
    agent = Agent(config, SkillLoader(builtin_skills_dir()))
    return CommandContext(session=ChatSession(agent), registry=registry), agent


class RenderTemplateTests(unittest.TestCase):
    def test_substitutes_arguments_placeholder(self) -> None:
        self.assertEqual(render_template("review $ARGUMENTS please", "a.py b.py"), "review a.py b.py please")

    def test_substitutes_positional_placeholders(self) -> None:
        self.assertEqual(render_template("$1 then $2", "first second"), "first then second")

    def test_missing_positional_becomes_empty(self) -> None:
        self.assertEqual(render_template("only $1|$2", "solo"), "only solo|")

    def test_appends_args_when_no_placeholder(self) -> None:
        self.assertEqual(render_template("Summarize this", "the readme"), "Summarize this\n\nthe readme")

    def test_no_placeholder_no_args_is_unchanged(self) -> None:
        self.assertEqual(render_template("Just do it", ""), "Just do it")


class FrontmatterTests(unittest.TestCase):
    def test_parses_description(self) -> None:
        meta, body = parse_frontmatter("---\ndescription: hi there\n---\nbody line\n")
        self.assertEqual(meta["description"], "hi there")
        self.assertEqual(body.strip(), "body line")

    def test_no_frontmatter_returns_full_body(self) -> None:
        meta, body = parse_frontmatter("just a body")
        self.assertEqual(meta, {})
        self.assertEqual(body, "just a body")


class LoadCustomCommandsTests(unittest.TestCase):
    def test_project_overrides_global_on_name_clash(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            global_dir = root / "global"
            project.mkdir()
            global_dir.mkdir()
            (global_dir / "hello.md").write_text("---\ndescription: g\n---\nglobal body", encoding="utf-8")
            (project / "hello.md").write_text("---\ndescription: p\n---\nproject body", encoding="utf-8")

            commands = load_custom_commands([("global", global_dir), ("project", project)])

        self.assertEqual(commands["hello"].scope, "project")
        self.assertEqual(commands["hello"].template, "project body")

    def test_missing_dirs_are_ignored(self) -> None:
        commands = load_custom_commands([("project", Path("does-not-exist-xyz"))])
        self.assertEqual(commands, {})


class DispatchTests(unittest.TestCase):
    def test_non_slash_input_is_not_a_command(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx, _ = _context(registry, root)
            self.assertIsNone(registry.dispatch(ctx, "hello there"))

    def test_double_slash_escapes_to_agent_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx, _ = _context(registry, root)
            result = registry.dispatch(ctx, "//not a command")
            assert result is not None
            self.assertEqual(result.agent_prompt, "/not a command")

    def test_exit_sets_should_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx, _ = _context(registry, root)
            self.assertTrue(registry.dispatch(ctx, "/exit").should_exit)
            self.assertTrue(registry.dispatch(ctx, "/quit").should_exit)

    def test_help_lists_builtin_and_custom(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            commands = root / "commands"
            commands.mkdir()
            (commands / "review.md").write_text("---\ndescription: review a file\n---\nreview $ARGUMENTS", encoding="utf-8")
            registry = _make_registry(commands, root / "global")
            ctx, _ = _context(registry, root)
            output = registry.dispatch(ctx, "/help").output
            self.assertIn("/skills", output)
            self.assertIn("/review", output)
            self.assertIn("review a file", output)

    def test_unknown_command_does_not_reach_agent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx, _ = _context(registry, root)
            result = registry.dispatch(ctx, "/nope")
            self.assertIsNone(result.agent_prompt)
            self.assertIn("Unknown command", result.output)

    def test_custom_command_expands_to_agent_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            commands = root / "commands"
            commands.mkdir()
            (commands / "greet.md").write_text("Say hi to $ARGUMENTS", encoding="utf-8")
            registry = _make_registry(commands, root / "global")
            ctx, _ = _context(registry, root)
            result = registry.dispatch(ctx, "/greet Ada")
            self.assertEqual(result.agent_prompt, "Say hi to Ada")

    def test_bare_slash_shows_help(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx, _ = _context(registry, root)
            result = registry.dispatch(ctx, "/")
            self.assertIn("Built-in commands", result.output)

    def test_reload_picks_up_new_command(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            commands = root / "commands"
            commands.mkdir()
            registry = _make_registry(commands, root / "global")
            ctx, _ = _context(registry, root)
            self.assertIn("Unknown command", registry.dispatch(ctx, "/fresh x").output)
            (commands / "fresh.md").write_text("do $ARGUMENTS", encoding="utf-8")
            registry.dispatch(ctx, "/reload")
            self.assertEqual(registry.dispatch(ctx, "/fresh x").agent_prompt, "do x")


class AgentCommandTests(unittest.TestCase):
    def _ctx(self, registry: CommandRegistry, root: Path, active: str | None = None) -> CommandContext:
        config = AgentConfig(
            model="local",
            workspace=root.resolve(),
            require_confirmation=False,
            openai_api_key=None,
            openai_base_url="https://api.openai.com/v1",
            enable_logging=False,
            skill_library_dir=builtin_skills_dir(),
            agents_dir=(root / "agents").resolve(),
        )
        agent = Agent(config, SkillLoader(builtin_skills_dir()))
        return CommandContext(
            session=ChatSession(agent), registry=registry, active_agent=active
        )

    def test_agents_lists_builtin_default_when_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx = self._ctx(registry, root)
            output = registry.dispatch(ctx, "/agents").output
            self.assertIn("default", output)
            self.assertIn("built-in", output)

    def test_agents_lists_created_with_active_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_profile(root / "agents", "office", skills=["datetime"])
            registry = _make_registry(root / "commands", root / "global")
            ctx = self._ctx(registry, root, active="office")
            output = registry.dispatch(ctx, "/agents").output
            self.assertIn("office", output)
            self.assertIn("*", output)

    def test_agent_no_arg_shows_active(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx = self._ctx(registry, root, active="office")
            self.assertIn("office", registry.dispatch(ctx, "/agent").output)

    def test_agent_switch_calls_activate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root / "commands", root / "global")
            ctx = self._ctx(registry, root)
            called: list[str] = []

            def fake_activate(name: str) -> str:
                called.append(name)
                return f"switched {name}"

            ctx.activate = fake_activate
            result = registry.dispatch(ctx, "/agent office")
            self.assertEqual(called, ["office"])
            self.assertIn("switched office", result.output)


if __name__ == "__main__":
    unittest.main()
