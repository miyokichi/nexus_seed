"""The bundled skill library: loading it, and running what it registers.

Skills ship inside the package (``little_agent/builtin_skills/``) and reach the
runtime only through the loader, so
these tests treat the library as data: every folder must load, every manifest
must produce working tools, and a new skill must need nothing but a folder.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import little_agent
from little_agent.config import (
    AgentConfig,
    builtin_skills_dir,
    describe_skill_library,
    resolve_skill_library,
)
from little_agent.skills.loader import SkillLoader
from little_agent.tools.base import ToolContext

LIBRARY = builtin_skills_dir()


class BundledLibraryTests(unittest.TestCase):
    def test_every_bundled_skill_folder_loads(self) -> None:
        folders = {
            path.parent.name for path in LIBRARY.glob("*/SKILL.md")
        }
        loaded = {skill.path.parent.name for skill in SkillLoader(LIBRARY).load_all()}
        self.assertEqual(loaded, folders)

    def test_the_documented_skills_are_present(self) -> None:
        folders = {path.parent.name for path in LIBRARY.glob("*/SKILL.md")}
        self.assertGreaterEqual(
            folders,
            {
                "agent_manager",
                "excel_file",
                "file_manager",
                "ppt_file",
                "project_manager",
                "python_coder",
                "windows_operator",
                "workspace_harness",
            },
        )

    def test_every_manifest_yields_tools_with_an_existing_script(self) -> None:
        tools = SkillLoader(LIBRARY).load_tools()
        self.assertGreater(len(tools), 0)
        for tool in tools:
            self.assertTrue(tool.script_path.exists(), tool.script_path)
            self.assertEqual(tool.parameters.get("type"), "object", tool.name)
            self.assertTrue(tool.description.strip(), tool.name)

    def test_tool_names_are_unique_across_the_library(self) -> None:
        names = [tool.name for tool in SkillLoader(LIBRARY).load_tools()]
        duplicates = {name for name in names if names.count(name) > 1}
        self.assertEqual(duplicates, set())

    def test_no_skill_script_imports_the_core_package(self) -> None:
        """Skills must stay copy-portable: the dependency runs one way only."""

        # Matches a real import statement, not the prose in a comment saying
        # scripts must not import little_agent.
        importing = re.compile(r"^\s*(?:import|from)\s+little_agent", re.MULTILINE)
        offenders = [
            str(path.relative_to(LIBRARY))
            for path in LIBRARY.glob("*/scripts/*.py")
            if importing.search(path.read_text(encoding="utf-8", errors="replace"))
        ]
        self.assertEqual(offenders, [])


class SkillToolExecutionTests(unittest.TestCase):
    """Run real skill tools through the script protocol."""

    def setUp(self) -> None:
        self.tools = {tool.name: tool for tool in SkillLoader(LIBRARY).load_tools()}

    def test_get_datetime_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            result = self.tools["get_datetime"].run(ToolContext(Path(tmp).resolve()))
            self.assertTrue(result.ok, result.content)
            self.assertTrue(result.content.strip())

    def test_harness_overview_reads_the_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tasks" / "billing" / "invoice-check").mkdir(parents=True)
            (root / "tasks" / "billing" / "invoice-check" / "task.md").write_text(
                "---\nstatus: todo\nassignee: ai\n---\n\nCheck the invoice.\n",
                encoding="utf-8",
            )
            result = self.tools["harness_overview"].run(ToolContext(root))

            self.assertTrue(result.ok, result.content)
            self.assertIn("billing", result.content)

    def test_a_skill_tool_returning_non_ascii_survives_the_pipe(self) -> None:
        """The script protocol is UTF-8 on both sides, whatever the console is."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            area = root / "tasks" / "経理"
            area.mkdir(parents=True)
            (area / "請求書").mkdir()
            (area / "請求書" / "task.md").write_text(
                "---\nstatus: todo\n---\n\n請求書を確認する\n", encoding="utf-8"
            )
            result = self.tools["harness_overview"].run(ToolContext(root))

            self.assertTrue(result.ok, result.content)
            self.assertIn("経理", result.content)

    def test_a_project_tool_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            ctx = ToolContext(Path(tmp).resolve())
            created = self.tools["create_project"].run(ctx, title="Q3 report")
            self.assertTrue(created.ok, created.content)

            listed = self.tools["list_projects"].run(ctx)
            self.assertTrue(listed.ok, listed.content)
            self.assertIn("Q3 report", listed.content)


class SkillLibraryResolutionTests(unittest.TestCase):
    """Skills are a runtime resource, resolved independently of the workspace.

    Order: an explicit LITTLE_AGENT_SKILL_LIBRARY_DIR, then <workspace>/skills
    when it exists, then the library shipped inside the package.
    """

    def test_builtin_library_ships_inside_the_package(self) -> None:
        builtin = builtin_skills_dir()
        self.assertTrue(builtin.is_dir(), builtin)
        # It lives under the package, which is what makes pip carry it.
        package_root = Path(little_agent.__file__).resolve().parent
        self.assertEqual(builtin.parent, package_root)
        self.assertGreater(len(list(builtin.glob("*/SKILL.md"))), 5)

    def test_an_external_workspace_still_gets_the_builtin_skills(self) -> None:
        """The regression this ordering exists for: skills must not vanish."""

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "my-project"
            workspace.mkdir()
            self.assertEqual(resolve_skill_library(None, workspace), builtin_skills_dir())
            skills = SkillLoader(resolve_skill_library(None, workspace)).load_all()
            self.assertGreater(len(skills), 5)

    def test_a_workspace_library_takes_precedence_when_present(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "skills" / "local_only").mkdir(parents=True)
            (workspace / "skills" / "local_only" / "SKILL.md").write_text(
                "# local_only\n\n## Description\nWorkspace-specific.\n", encoding="utf-8"
            )
            resolved = resolve_skill_library(None, workspace)
            self.assertEqual(resolved, workspace / "skills")
            self.assertEqual([s.name for s in SkillLoader(resolved).load_all()], ["local_only"])

    def test_an_explicit_setting_wins_over_both(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            # A workspace library exists, and would win if nothing were set.
            (workspace / "skills" / "ignored").mkdir(parents=True)
            (workspace / "skills" / "ignored" / "SKILL.md").write_text(
                "# ignored\n\n## Description\nx\n", encoding="utf-8"
            )
            chosen = workspace / "elsewhere"
            (chosen / "chosen_skill").mkdir(parents=True)
            (chosen / "chosen_skill" / "SKILL.md").write_text(
                "# chosen_skill\n\n## Description\nx\n", encoding="utf-8"
            )
            resolved = resolve_skill_library(str(chosen), workspace)
            self.assertEqual(resolved, chosen)
            self.assertEqual([s.name for s in SkillLoader(resolved).load_all()], ["chosen_skill"])

    def test_an_explicit_relative_setting_resolves_from_the_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self.assertEqual(
                resolve_skill_library("my-skills", workspace), workspace / "my-skills"
            )

    def test_an_explicit_setting_is_honoured_even_when_missing(self) -> None:
        """A typo must surface as an empty library, not fall back silently."""

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "skills").mkdir()
            resolved = resolve_skill_library(str(workspace / "typo"), workspace)
            self.assertEqual(resolved, workspace / "typo")
            self.assertNotEqual(resolved, builtin_skills_dir())

    def test_blank_setting_is_treated_as_unset(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self.assertEqual(resolve_skill_library("   ", workspace), builtin_skills_dir())

    def test_config_from_env_uses_the_builtin_library_for_an_external_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("LITTLE_AGENT_")
            }
            env["LITTLE_AGENT_WORKSPACE"] = str(workspace)
            with mock.patch.dict(os.environ, env, clear=True):
                config = AgentConfig.from_env()
            self.assertEqual(config.skill_library_dir, builtin_skills_dir())
            self.assertIn("built-in", describe_skill_library(config))

    def test_describe_names_which_rung_of_the_order_won(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "skills").mkdir()
            base = dict(
                model="local",
                workspace=workspace,
                require_confirmation=False,
                openai_api_key=None,
                openai_base_url="https://api.openai.com/v1",
                enable_logging=False,
                agents_dir=workspace / "agents",
            )
            self.assertIn(
                "(built-in)",
                describe_skill_library(AgentConfig(**base, skill_library_dir=builtin_skills_dir())),
            )
            self.assertIn(
                "(workspace)",
                describe_skill_library(
                    AgentConfig(**base, skill_library_dir=workspace / "skills")
                ),
            )
            self.assertIn(
                "(configured)",
                describe_skill_library(
                    AgentConfig(**base, skill_library_dir=workspace / "elsewhere")
                ),
            )


class EmptyLibraryWarningTests(unittest.TestCase):
    """An agent with no skills must say so rather than start quietly."""

    def test_a_missing_library_warns_and_names_where_it_looked(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-there"
            warning = SkillLoader(missing).warning()
            self.assertIsNotNone(warning)
            self.assertIn("no skills were loaded", warning)
            self.assertIn(str(missing), warning)
            self.assertIn("do not exist", warning)
            self.assertIn("LITTLE_AGENT_SKILL_LIBRARY_DIR", warning)

    def test_an_empty_but_existing_library_warns(self) -> None:
        with TemporaryDirectory() as tmp:
            empty = Path(tmp) / "skills"
            empty.mkdir()
            warning = SkillLoader(empty).warning()
            self.assertIsNotNone(warning)
            self.assertNotIn("do not exist", warning)

    def test_a_populated_library_does_not_warn(self) -> None:
        self.assertIsNone(SkillLoader(builtin_skills_dir()).warning())

    def test_a_profile_asking_for_no_skills_is_not_warned_about(self) -> None:
        """Declaring "skills": [] is a choice, not a misconfiguration."""

        self.assertIsNone(SkillLoader(builtin_skills_dir(), names=set()).warning())

    def test_a_profile_whose_named_skills_are_missing_is_warned_about(self) -> None:
        with TemporaryDirectory() as tmp:
            empty = Path(tmp) / "skills"
            empty.mkdir()
            warning = SkillLoader(empty, names={"datetime", "excel_file"}).warning()
            self.assertIsNotNone(warning)
            self.assertIn("datetime", warning)
            self.assertIn("excel_file", warning)


class NewSkillTests(unittest.TestCase):
    """Adding a skill is adding a folder — no core change, no registration."""

    def test_a_dropped_in_folder_becomes_a_loadable_skill_with_a_tool(self) -> None:
        with TemporaryDirectory() as tmp:
            library = Path(tmp) / "skills"
            skill = library / "greeter"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "# greeter\n\n## Description\nSays hello.\n\n"
                "## When to use\nWhen greeting someone.\n\n"
                "## Allowed tools\n- greet\n\n## Instructions\nBe brief.\n",
                encoding="utf-8",
            )
            (skill / "scripts" / "greet.py").write_text(
                "import json, sys\n"
                "payload = json.loads(sys.stdin.read() or '{}')\n"
                "name = payload['arguments'].get('name', 'world')\n"
                "print(json.dumps({'ok': True, 'content': f'hello {name}'}))\n",
                encoding="utf-8",
            )
            (skill / "tools.json").write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "greet",
                                "description": "Greet someone.",
                                "script": "scripts/greet.py",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "additionalProperties": False,
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            loader = SkillLoader(library)
            self.assertEqual([s.name for s in loader.load_all()], ["greeter"])

            tools = {tool.name: tool for tool in loader.load_tools()}
            result = tools["greet"].run(ToolContext(library), name="Ada")
            self.assertTrue(result.ok, result.content)
            self.assertEqual(result.content, "hello Ada")

    def test_a_script_outside_its_skill_folder_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            library = Path(tmp) / "skills"
            skill = library / "escapee"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# escapee\n\n## Description\nx\n", encoding="utf-8")
            (library / "outside.py").write_text("print('{}')", encoding="utf-8")
            (skill / "tools.json").write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "escape",
                                "description": "x",
                                "script": "../outside.py",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            # The manifest is skipped rather than trusted; the skill still loads.
            self.assertEqual(SkillLoader(library).load_tools(), [])
            self.assertEqual([s.name for s in SkillLoader(library).load_all()], ["escapee"])


if __name__ == "__main__":
    unittest.main()
