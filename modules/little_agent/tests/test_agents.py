"""Agent profiles: the capability contract that bounds what an agent may do."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.config import builtin_skills_dir
from little_agent.config import AgentConfig
from little_agent.control import StopController
from little_agent.factory import build_agent
from little_agent.tools import default_tools

LIBRARY = builtin_skills_dir()


def _config(workspace: Path, agents_dir: Path, **overrides) -> AgentConfig:
    base = dict(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
        skill_library_dir=LIBRARY,
        agents_dir=agents_dir.resolve(),
    )
    base.update(overrides)
    return AgentConfig(**base)  # type: ignore[arg-type]


def write_profile(agents_dir: Path, name: str, **profile) -> Path:
    """Create an agents/<name>/agent.json, the way an operator would."""

    directory = agents_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.json").write_text(
        json.dumps({"name": name, **profile}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return directory


class DefaultToolsFilterTests(unittest.TestCase):
    def test_none_registers_all_core_tools(self) -> None:
        self.assertIn("run_powershell", default_tools().names())

    def test_allowlist_registers_only_named_tools(self) -> None:
        registry = default_tools({"read_file", "list_dir"})
        self.assertEqual(set(registry.names()), {"read_file", "list_dir"})


class ProfileLoadingTests(unittest.TestCase):
    def test_profile_round_trips_from_disk(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            write_profile(
                agents_dir,
                "reader",
                description="reads things",
                model="gpt-test",
                skills=["datetime"],
                core_tools=["read_file"],
                max_tool_steps=2,
                require_confirmation=True,
            )

            profile = agents.load_profile(agents_dir, "reader", LIBRARY)

            self.assertEqual(profile.description, "reads things")
            self.assertEqual(profile.model, "gpt-test")
            self.assertEqual(profile.core_tools_set(), {"read_file"})
            self.assertEqual(profile.max_tool_steps, 2)
            self.assertTrue(profile.require_confirmation)
            self.assertEqual(profile.enabled_skills(), ["datetime"])

    def test_declared_skills_come_from_the_library(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            write_profile(agents_dir, "observer", skills=["datetime", "excel_file"])

            profile = agents.load_profile(agents_dir, "observer", LIBRARY)

            self.assertEqual(profile.enabled_skills(), ["datetime", "excel_file"])
            self.assertIn(LIBRARY, profile.skill_roots())

    def test_own_skill_folder_is_used_and_shadows_the_library(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            directory = write_profile(agents_dir, "solo")
            shutil.copytree(LIBRARY / "datetime", directory / "skills" / "datetime")

            profile = agents.load_profile(agents_dir, "solo", LIBRARY)

            # No "skills" declaration: only the profile's own folder is searched.
            self.assertEqual(profile.enabled_skills(), ["datetime"])
            self.assertEqual(profile.skill_roots(), [directory / "skills"])

    def test_missing_agent_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                agents.load_profile(Path(tmp) / "agents", "nope", LIBRARY)

    def test_agent_name_cannot_escape_the_agents_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"

            resolved = agents.agent_dir(agents_dir, "../secret")

            self.assertEqual(resolved, (agents_dir / "secret").resolve())

    def test_list_agents(self) -> None:
        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            write_profile(agents_dir, "a")
            write_profile(agents_dir, "b")
            (agents_dir / "not-an-agent").mkdir()

            self.assertEqual(agents.list_agents(agents_dir), ["a", "b"])

    def test_resolve_active(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            config = _config(root, agents_dir)
            # No request / reserved names resolve to the built-in default agent.
            self.assertEqual(agents.resolve_active(config, None).name, agents.DEFAULT_AGENT_NAME)
            self.assertTrue(agents.resolve_active(config, "default").builtin)
            self.assertTrue(agents.resolve_active(config, "library").builtin)
            with self.assertRaises(FileNotFoundError):
                agents.resolve_active(config, "missing")
            write_profile(agents_dir, "here", skills=["datetime"])
            self.assertEqual(agents.resolve_active(config, "here").name, "here")

    def test_default_profile_uses_the_whole_library(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            default = agents.default_profile(_config(root, root / "agents"))

            self.assertTrue(default.builtin)
            self.assertEqual(default.skill_roots(), [LIBRARY])
            self.assertIsNone(default.skill_names())
            self.assertIn("datetime", default.enabled_skills())
            self.assertIsNone(default.core_tools_set())


class ProfileCapabilityTests(unittest.TestCase):
    """A profile decides which skills and tools an agent actually gets."""

    def _build(self, root: Path, name: str | None, **profile):
        agents_dir = root / "agents"
        config = _config(root, agents_dir)
        if name is None:
            resolved = agents.default_profile(config)
        else:
            write_profile(agents_dir, name, **profile)
            resolved = agents.load_profile(agents_dir, name, LIBRARY)
        return build_agent(config, resolved, lambda *_: True, StopController("x"))

    def test_skills_are_limited_to_the_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = self._build(Path(tmp), "narrow", skills=["datetime"])

            self.assertEqual([skill.name for skill in agent.skills.load_all()], ["datetime"])
            self.assertIn("get_datetime", agent.tools.names())
            self.assertNotIn("read_excel", agent.tools.names())  # excel_file not granted

    def test_core_tool_allowlist_is_applied(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = self._build(Path(tmp), "tooled", skills=["datetime"], core_tools=["read_file"])
            names = set(agent.tools.names())

            self.assertIn("read_file", names)  # allowed core tool
            self.assertNotIn("run_powershell", names)  # filtered-out core tool
            self.assertIn("get_datetime", names)  # skill script tools are unaffected

    def test_delegation_is_granted_by_default_and_deniable_by_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            open_agent = self._build(root, "open", skills=["datetime"])
            self.assertIn("delegate_task", open_agent.tools.names())
            self.assertIn("delegate_tasks", open_agent.tools.names())

            closed = self._build(root, "closed", skills=["datetime"], core_tools=["read_file"])
            self.assertNotIn("delegate_task", closed.tools.names())
            self.assertNotIn("delegate_tasks", closed.tools.names())

            partial = self._build(
                root, "partial", skills=["datetime"], core_tools=["read_file", "delegate_task"]
            )
            self.assertIn("delegate_task", partial.tools.names())
            self.assertNotIn("delegate_tasks", partial.tools.names())

    def test_profile_overrides_model_and_step_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = self._build(
                Path(tmp), "slow", skills=["datetime"], model="gpt-test", max_tool_steps=9
            )

            self.assertEqual(agent.config.model, "gpt-test")
            self.assertEqual(agent.config.max_tool_steps, 9)

    def test_default_agent_gets_the_library_and_every_core_tool(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = self._build(Path(tmp), None)
            names = set(agent.tools.names())

            self.assertIn("run_powershell", names)
            self.assertIn("get_datetime", names)
            self.assertIn("delegate_task", names)


if __name__ == "__main__":
    unittest.main()
