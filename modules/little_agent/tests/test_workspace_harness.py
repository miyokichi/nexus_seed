import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from little_agent.config import builtin_skills_dir

SCRIPT = builtin_skills_dir() / "workspace_harness" / "scripts" / "harness_tool.py"


def call(tool: str, workspace: Path, arguments: dict) -> dict:
    """Invoke the skill script the way ScriptSkillTool does.

    The UTF-8 pinning matters: this skill answers in Japanese, which a console
    codepage such as cp932 cannot always encode.
    """

    payload = {"tool": tool, "workspace": str(workspace), "arguments": arguments}
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), tool],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=True,
    )
    return json.loads(completed.stdout.strip())


class WorkspaceHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        # A human-created top-level area and a shared material.
        (self.ws / "tasks" / "営業").mkdir(parents=True)
        (self.ws / "shared" / "templates").mkdir(parents=True)
        (self.ws / "shared" / "templates" / "quote.md").write_text(
            "見積テンプレート\n合計金額を確認する", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_task_in_existing_area(self) -> None:
        result = call("create_task_folder", self.ws, {"area": "営業", "title": "見積書 作成", "assignee": "ai"})
        self.assertTrue(result["ok"], result)
        task_md = self.ws / "tasks" / "営業" / "見積書-作成" / "task.md"
        self.assertTrue(task_md.is_file())
        self.assertIn("assignee: ai", task_md.read_text(encoding="utf-8"))
        self.assertTrue((self.ws / "tasks" / "営業" / "見積書-作成" / "outputs").is_dir())

    def test_create_task_refuses_unknown_area(self) -> None:
        result = call("create_task_folder", self.ws, {"area": "存在しない", "title": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("propose_area", result["content"])
        self.assertFalse((self.ws / "tasks" / "存在しない").exists())

    def test_propose_area_does_not_create_folder(self) -> None:
        result = call("propose_area", self.ws, {"name": "開発", "reason": "コードタスク増加"})
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.ws / "tasks" / "開発").exists())
        proposals = (self.ws / "tasks" / "PROPOSALS.md").read_text(encoding="utf-8")
        self.assertIn("開発", proposals)

    def test_update_status_and_note(self) -> None:
        call("create_task_folder", self.ws, {"area": "営業", "title": "T", "assignee": "ai"})
        upd = call("update_task_folder", self.ws, {"task": "営業/t", "status": "doing"})
        self.assertTrue(upd["ok"], upd)
        note = call("add_task_note", self.ws, {"task": "t", "text": "着手"})
        self.assertTrue(note["ok"], note)
        text = (self.ws / "tasks" / "営業" / "t" / "task.md").read_text(encoding="utf-8")
        self.assertIn("status: doing", text)
        self.assertIn("着手", text)

    def test_search_shared_matches_name_and_content(self) -> None:
        result = call("search_shared", self.ws, {"query": "見積"})
        self.assertTrue(result["ok"], result)
        self.assertIn("quote.md", result["content"])

    def test_read_shared_rejects_escape(self) -> None:
        result = call("read_shared", self.ws, {"path": "../tasks/README.md"})
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["content"])


class SkillToolNameUniquenessTests(unittest.TestCase):
    def test_no_duplicate_tool_names_across_skills(self) -> None:
        # The registry keys tools by name, so a duplicate silently shadows
        # another skill's tool. Guard against that regression.
        from collections import Counter

        from little_agent.skills.loader import SkillLoader

        names = [tool.name for tool in SkillLoader(builtin_skills_dir()).load_tools()]
        duplicates = [name for name, count in Counter(names).items() if count > 1]
        self.assertEqual(duplicates, [], f"Duplicate skill tool names: {duplicates}")

    def test_workspace_harness_is_loaded(self) -> None:
        from little_agent.skills.loader import SkillLoader

        skills = {s.name for s in SkillLoader(builtin_skills_dir()).load_all()}
        self.assertIn("workspace_harness", skills)


if __name__ == "__main__":
    unittest.main()
