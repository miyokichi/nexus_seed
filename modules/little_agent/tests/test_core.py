from contextlib import suppress
import json
import os
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path

from little_agent.config import builtin_skills_dir
from little_agent.agent import Agent, StructuredOutputError
from little_agent.config import AgentConfig
from little_agent.llm import OpenAICompatibleChatClient
from little_agent.messages import Message
from little_agent.skills.loader import SkillLoader
from little_agent.tools import default_tools
from little_agent.tools.base import ToolContext, ToolRegistry, ToolResult, resolve_workspace_path

LIBRARY = builtin_skills_dir()


def _config(workspace: Path | None = None, **overrides) -> AgentConfig:
    base = dict(
        model="local",
        workspace=(workspace or Path.cwd()).resolve(),
        require_confirmation=True,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
    )
    base.update(overrides)
    return AgentConfig(**base)  # type: ignore[arg-type]


class RecordingLLM:
    """Replies from a scripted list, remembering the messages it was given."""

    def __init__(self, *replies: dict[str, object]) -> None:
        self.replies = list(replies)
        self.calls: list[list[Message]] = []

    def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict[str, object]:
        self.calls.append([*messages])
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return {"content": "", "tool_calls": [], **reply}


class SkillAndToolTests(unittest.TestCase):
    def test_skill_loader_reads_sample_skills(self) -> None:
        skills = SkillLoader(LIBRARY).load_all()

        self.assertGreaterEqual(
            {skill.name for skill in skills},
            {
                "file_manager",
                "python_coder",
                "windows_operator",
                "skill_creator",
                "excel_file",
                "ppt_file",
            },
        )

    def test_skill_loader_can_restrict_to_named_skills(self) -> None:
        skills = SkillLoader(LIBRARY, names={"datetime"}).load_all()

        self.assertEqual([skill.name for skill in skills], ["datetime"])

    def test_skill_loader_first_root_shadows_later_ones(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "skills" / "datetime"
            own.mkdir(parents=True)
            (own / "SKILL.md").write_text("# datetime\n\n## Description\nlocal override\n", encoding="utf-8")

            skills = SkillLoader([own.parent, LIBRARY], names={"datetime"}).load_all()

        self.assertEqual([skill.name for skill in skills], ["datetime"])
        self.assertIn("local override", skills[0].description)

    def test_skill_loader_reads_script_tools(self) -> None:
        names = {tool.name for tool in SkillLoader(LIBRARY).load_tools()}

        for expected in (
            "get_datetime",
            "create_skill",
            "validate_skill",
            "read_excel",
            "write_excel",
            "read_ppt",
            "write_ppt",
            "take_screenshot",
        ):
            self.assertIn(expected, names)

    def test_agent_registers_skill_script_tools(self) -> None:
        agent = Agent(_config(), SkillLoader(LIBRARY))

        self.assertIn("get_datetime", agent.tools.names())
        self.assertIn("create_skill", agent.tools.names())
        self.assertIn("read_excel", agent.tools.names())

    def test_workspace_path_rejects_parent_escape(self) -> None:
        workspace = (Path.cwd() / ".test-workspace").resolve()

        with self.assertRaisesRegex(ValueError, "outside allowed read paths"):
            resolve_workspace_path(workspace, "..")

    def test_filesystem_tools_honor_extra_readable_and_writable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            readable = root / "readable"
            writable = root / "writable"
            outside = root / "outside"
            for directory in (workspace, readable, writable, outside):
                directory.mkdir()
            (workspace / "in.txt").write_text("workspace", encoding="utf-8")
            (readable / "in.txt").write_text("readable", encoding="utf-8")
            (writable / "in.txt").write_text("writable", encoding="utf-8")
            (outside / "in.txt").write_text("outside", encoding="utf-8")

            context = ToolContext(
                workspace=workspace,
                readable_paths=(readable,),
                writable_paths=(writable,),
            )
            tools = default_tools()

            workspace_read = tools.get("read_file").run(context, path="in.txt")
            workspace_write = tools.get("write_file").run(context, path="out.txt", content="ok")
            readable_read = tools.get("read_file").run(context, path=str(readable / "in.txt"))
            writable_read = tools.get("read_file").run(context, path=str(writable / "in.txt"))
            writable_write = tools.get("write_file").run(
                context,
                path=str(writable / "out.txt"),
                content="ok",
            )

            with self.assertRaisesRegex(ValueError, "outside allowed write paths"):
                tools.get("write_file").run(context, path=str(readable / "out.txt"), content="no")
            with self.assertRaisesRegex(ValueError, "outside allowed read paths"):
                tools.get("read_file").run(context, path=str(outside / "in.txt"))
            with self.assertRaisesRegex(ValueError, "outside allowed write paths"):
                tools.get("write_file").run(context, path=str(outside / "out.txt"), content="no")

            self.assertTrue(workspace_read.ok)
            self.assertTrue(workspace_write.ok)
            self.assertTrue(readable_read.ok)
            self.assertTrue(writable_read.ok)
            self.assertTrue(writable_write.ok)
            self.assertEqual((workspace / "out.txt").read_text(encoding="utf-8"), "ok")
            self.assertEqual((writable / "out.txt").read_text(encoding="utf-8"), "ok")

    def test_file_level_readable_and_writable_paths_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            readable_file = root / "readable.txt"
            writable_file = root / "writable.txt"
            readable_file.write_text("readable", encoding="utf-8")
            writable_file.write_text("writable", encoding="utf-8")

            context = ToolContext(
                workspace=workspace,
                readable_paths=(readable_file,),
                writable_paths=(writable_file,),
            )
            tools = default_tools()

            self.assertEqual(tools.get("read_file").run(context, path=str(readable_file)).content, "readable")
            with self.assertRaisesRegex(ValueError, "outside allowed read paths"):
                tools.get("read_file").run(context, path=str(readable_file / "child.txt"))
            with self.assertRaisesRegex(ValueError, "outside allowed write paths"):
                tools.get("write_file").run(context, path=str(readable_file), content="no")

            self.assertEqual(tools.get("read_file").run(context, path=str(writable_file)).content, "writable")
            written = tools.get("write_file").run(context, path=str(writable_file), content="updated")
            with self.assertRaisesRegex(ValueError, "outside allowed write paths"):
                tools.get("write_file").run(context, path=str(writable_file / "child.txt"), content="no")

            self.assertTrue(written.ok)
            self.assertEqual(writable_file.read_text(encoding="utf-8"), "updated")

    def test_path_traversal_and_symlink_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            allowed = root / "allowed"
            outside = root / "outside"
            for directory in (workspace, allowed, outside):
                directory.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            context = ToolContext(workspace=workspace, readable_paths=(allowed,), writable_paths=(allowed,))
            tools = default_tools()

            with self.assertRaisesRegex(ValueError, "outside allowed read paths"):
                tools.get("read_file").run(context, path="../outside/secret.txt")

            link = allowed / "secret-link.txt"
            try:
                os.symlink(outside / "secret.txt", link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "outside allowed read paths"):
                tools.get("read_file").run(context, path=str(link))

    def test_filesystem_append_delete_move_tools(self) -> None:
        workspace = (Path.cwd() / ".test-fs-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = default_tools()

        try:
            tools.get("write_file").run(context, path="a.txt", content="hello")
            appended = tools.get("append_to_file").run(context, path="a.txt", content=" world")
            content_after_append = (workspace / "a.txt").read_text(encoding="utf-8")

            moved = tools.get("move_file").run(context, source="a.txt", destination="sub/b.txt")
            source_gone = not (workspace / "a.txt").exists()
            dest_exists = (workspace / "sub" / "b.txt").exists()

            deleted = tools.get("delete_file").run(context, path="sub/b.txt")
            dest_gone = not (workspace / "sub" / "b.txt").exists()
        finally:
            for path in [workspace / "a.txt", workspace / "sub" / "b.txt"]:
                path.unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "sub").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(appended.ok)
        self.assertEqual(content_after_append, "hello world")
        self.assertTrue(moved.ok)
        self.assertTrue(source_gone)
        self.assertTrue(dest_exists)
        self.assertTrue(deleted.ok)
        self.assertTrue(dest_gone)

    def test_delete_and_move_reject_missing_or_directory(self) -> None:
        workspace = (Path.cwd() / ".test-fs-guard-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = default_tools()

        try:
            (workspace / "data").mkdir(parents=True, exist_ok=True)
            missing = tools.get("delete_file").run(context, path="nope.txt")
            on_dir = tools.get("delete_file").run(context, path="data")
            move_missing = tools.get("move_file").run(context, source="nope.txt", destination="x.txt")
        finally:
            with suppress(OSError):
                (workspace / "data").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertFalse(missing.ok)
        self.assertFalse(on_dir.ok)
        self.assertFalse(move_missing.ok)

    def test_fetch_url_rejects_non_http_scheme(self) -> None:
        from little_agent.tools.web import FetchUrlTool

        result = FetchUrlTool().run(ToolContext(workspace=Path.cwd()), url="ftp://example.com")

        self.assertFalse(result.ok)
        self.assertIn("http(s)", result.content)

    def test_skill_creator_can_create_and_validate_skill(self) -> None:
        workspace = (Path.cwd() / ".test-skill-creator-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(LIBRARY).load_tools():
            tools.register(tool)

        try:
            created = tools.get("create_skill").run(
                context,
                name="Demo Skill",
                description="Demo skill for tests.",
                include_scripts=True,
                include_tools_manifest=True,
            )
            validated = tools.get("validate_skill").run(context, name="demo-skill")
        finally:
            for path in [
                workspace / "skills" / "demo-skill" / "scripts" / "example_tool.py",
                workspace / "skills" / "demo-skill" / "tools.json",
                workspace / "skills" / "demo-skill" / "SKILL.md",
            ]:
                path.unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "skills" / "demo-skill" / "scripts").rmdir()
            with suppress(OSError):
                (workspace / "skills" / "demo-skill").rmdir()
            with suppress(OSError):
                (workspace / "skills").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(created.ok)
        self.assertTrue(validated.ok)

    def test_excel_skill_can_write_and_read_xlsx(self) -> None:
        workspace = (Path.cwd() / ".test-excel-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(LIBRARY).load_tools():
            tools.register(tool)

        try:
            written = tools.get("write_excel").run(
                context,
                path="reports/sample.xlsx",
                sheet_name="Data",
                rows=[["Name", "Score"], ["Ada", 10], ["Grace", 12]],
            )
            read = tools.get("read_excel").run(context, path="reports/sample.xlsx")
        finally:
            (workspace / "reports" / "sample.xlsx").unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "reports").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(written.ok)
        self.assertTrue(read.ok)
        self.assertIn("Ada", read.content)
        self.assertIn("Grace", read.content)

    def test_ppt_skill_can_write_and_read_pptx(self) -> None:
        workspace = (Path.cwd() / ".test-ppt-workspace").resolve()
        context = ToolContext(workspace=workspace)
        tools = ToolRegistry()
        for tool in SkillLoader(LIBRARY).load_tools():
            tools.register(tool)

        try:
            written = tools.get("write_ppt").run(
                context,
                path="deck/sample.pptx",
                slides=[
                    {"title": "Roadmap", "bullets": ["Build skills", "Test tools"]},
                    {"title": "Next", "bullets": ["Iterate"]},
                ],
            )
            read = tools.get("read_ppt").run(context, path="deck/sample.pptx")
        finally:
            (workspace / "deck" / "sample.pptx").unlink(missing_ok=True)
            with suppress(OSError):
                (workspace / "deck").rmdir()
            with suppress(OSError):
                workspace.rmdir()

        self.assertTrue(written.ok)
        self.assertTrue(read.ok)
        self.assertIn("Roadmap", read.content)
        self.assertIn("Build skills", read.content)


class LLMClientTests(unittest.TestCase):
    def test_message_payload_passes_through_multimodal_content(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1")
        blocks = [
            {"type": "text", "text": "look:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        message = Message(role="user", content=blocks)

        payload = client._message_payload(message)

        self.assertEqual(payload["content"], blocks)

    def test_openai_compatible_client_payload_shape(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1")
        message = Message(role="user", content="hello")

        payload = client._message_payload(message)

        self.assertEqual(payload, {"role": "user", "content": "hello"})

    def test_openai_compatible_client_payload_shape_for_tool_loop(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1")
        assistant = Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "get_datetime", "arguments": {}}],
        )
        tool = Message(role="tool", content="OK: today", tool_call_id="call_1")

        assistant_payload = client._message_payload(assistant)
        tool_payload = client._message_payload(tool)

        self.assertEqual(assistant_payload["tool_calls"][0]["function"]["name"], "get_datetime")
        self.assertEqual(tool_payload["tool_call_id"], "call_1")

    def test_openai_compatible_client_parses_tool_calls(self) -> None:
        calls = OpenAICompatibleChatClient._tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "get_datetime", "arguments": "{}"},
                    }
                ]
            }
        )

        self.assertEqual(calls, [{"id": "call_1", "name": "get_datetime", "arguments": {}}])

    def test_openai_compatible_client_wraps_timeout(self) -> None:
        client = OpenAICompatibleChatClient("test-key", "http://localhost:1234/v1", timeout=1)

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "timed out after 1 seconds"):
                client._post_json("/chat/completions", {})


class AgentLoopTests(unittest.TestCase):
    def test_local_fallback_agent_can_use_datetime(self) -> None:
        agent = Agent(_config((Path.cwd() / ".test-workspace")), SkillLoader(LIBRARY))

        result = agent.run("date")

        self.assertIn("[get_datetime]", result.text)
        self.assertIn("OK:", result.text)
        self.assertIsNone(result.data)

    def test_agent_returns_tool_result_to_llm_for_final_answer(self) -> None:
        class TwoStepLLM:
            def __init__(self) -> None:
                self.calls: list[list[Message]] = []

            def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict[str, object]:
                self.calls.append([*messages])
                if len(self.calls) == 1:
                    return {
                        "content": "",
                        "tool_calls": [{"id": "call_1", "name": "get_datetime", "arguments": {}}],
                    }
                tool_messages = [message for message in messages if message.role == "tool"]
                return {"content": f"Final answer based on {tool_messages[-1].content}", "tool_calls": []}

        llm = TwoStepLLM()
        agent = Agent(_config(), SkillLoader(LIBRARY), llm=llm)  # type: ignore[arg-type]

        result = agent.run("date")

        self.assertIn("Final answer based on [get_datetime]", result.text)
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(any(message.role == "tool" for message in llm.calls[1]))

    def test_multi_step_tool_loop_runs_until_the_step_budget(self) -> None:
        class NeverFinishes:
            def __init__(self) -> None:
                self.steps = 0

            def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict[str, object]:
                self.steps += 1
                return {
                    "content": "",
                    "tool_calls": [{"id": f"c{self.steps}", "name": "get_datetime", "arguments": {}}],
                }

        llm = NeverFinishes()
        agent = Agent(_config(max_tool_steps=3), SkillLoader(LIBRARY), llm=llm)  # type: ignore[arg-type]

        result = agent.run("keep going")

        self.assertEqual(llm.steps, 3)
        self.assertIn("Stopped after 3 tool step(s)", result.text)

    def test_agent_forwards_tool_image_output_to_model(self) -> None:
        class ImageTool:
            name = "fake_capture"
            description = "Return an image."
            requires_confirmation = False
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}

            def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
                return ToolResult(True, "captured", images=("data:image/png;base64,AAAA",))

        registry = ToolRegistry()
        registry.register(ImageTool())
        llm = RecordingLLM(
            {"tool_calls": [{"id": "call_1", "name": "fake_capture", "arguments": {}}]},
            {"content": "I can see the image."},
        )
        agent = Agent(_config(require_confirmation=False), SkillLoader(LIBRARY), tools=registry, llm=llm)  # type: ignore[arg-type]

        result = agent.run("capture the screen")

        self.assertEqual(result.text, "I can see the image.")
        image_messages = [
            message
            for message in llm.calls[1]
            if message.role == "user"
            and isinstance(message.content, list)
            and any(block.get("type") == "image_url" for block in message.content)
        ]
        self.assertEqual(len(image_messages), 1)
        image_block = next(b for b in image_messages[0].content if b.get("type") == "image_url")
        self.assertEqual(image_block["image_url"]["url"], "data:image/png;base64,AAAA")

    def test_unknown_tool_is_reported_to_the_model(self) -> None:
        llm = RecordingLLM(
            {"tool_calls": [{"id": "call_1", "name": "no_such_tool", "arguments": {}}]},
            {"content": "understood"},
        )
        agent = Agent(_config(), SkillLoader(LIBRARY), tools=ToolRegistry(), llm=llm)  # type: ignore[arg-type]

        agent.run("do something")

        tool_messages = [message for message in llm.calls[1] if message.role == "tool"]
        self.assertIn("Unknown tool: no_such_tool", str(tool_messages[-1].content))

    def test_declined_confirmation_cancels_the_tool(self) -> None:
        calls: list[str] = []

        class WriteOnly:
            name = "needs_ok"
            description = "Needs confirmation."
            requires_confirmation = True
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}

            def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
                calls.append("ran")
                return ToolResult(True, "did it")

        registry = ToolRegistry()
        registry.register(WriteOnly())
        llm = RecordingLLM(
            {"tool_calls": [{"id": "call_1", "name": "needs_ok", "arguments": {}}]},
            {"content": "ok then"},
        )
        agent = Agent(
            _config(require_confirmation=True),
            SkillLoader(LIBRARY),
            tools=registry,
            llm=llm,  # type: ignore[arg-type]
            confirm=lambda _name, _args: False,
        )

        agent.run("write something")

        self.assertEqual(calls, [])
        tool_messages = [message for message in llm.calls[1] if message.role == "tool"]
        self.assertIn("Cancelled by user", str(tool_messages[-1].content))

    def test_agent_logs_conversation_tools_and_usage(self) -> None:
        workspace = (Path.cwd() / ".test-log-workspace").resolve()
        log_dir = workspace / "logs"
        agent = Agent(
            _config(workspace, enable_logging=True, log_dir=log_dir),
            SkillLoader(LIBRARY),
        )

        try:
            result = agent.run("date")
            assert agent.logger is not None
            session_id = agent.logger.session_id
            conversation_log = log_dir / "conversations" / f"{session_id}.jsonl"
            tool_log = log_dir / "tools" / f"{session_id}.jsonl"
            usage_log = log_dir / "usage" / f"{session_id}.jsonl"
            conversation_events = [
                json.loads(line)["event"] for line in conversation_log.read_text(encoding="utf-8").splitlines()
            ]
            tool_events = [json.loads(line)["event"] for line in tool_log.read_text(encoding="utf-8").splitlines()]
            usage_records = [json.loads(line) for line in usage_log.read_text(encoding="utf-8").splitlines()]
        finally:
            for category in ("conversations", "tools", "usage"):
                for path in (log_dir / category).glob("*.jsonl"):
                    path.unlink(missing_ok=True)
            for path in [log_dir / "conversations", log_dir / "tools", log_dir / "usage", log_dir, workspace]:
                with suppress(OSError):
                    path.rmdir()

        self.assertIn("[get_datetime]", result.text)
        self.assertIn("user_message", conversation_events)
        self.assertIn("final_answer", conversation_events)
        self.assertIn("tool_result", tool_events)
        self.assertGreaterEqual(usage_records[-1]["totals"]["total_tokens"], 1)


class StatelessnessTests(unittest.TestCase):
    """Nothing an execution sees or does may leak into the next one."""

    def test_second_run_sees_no_trace_of_the_first(self) -> None:
        llm = RecordingLLM({"content": "first"}, {"content": "second"})
        agent = Agent(_config(), SkillLoader(LIBRARY), llm=llm)  # type: ignore[arg-type]

        agent.run("remember the passphrase swordfish")
        agent.run("what was the passphrase?")

        self.assertEqual(len(llm.calls[0]), 2)  # system + user, nothing else
        self.assertEqual(len(llm.calls[1]), 2)
        self.assertNotIn("swordfish", "\n".join(str(m.content) for m in llm.calls[1]))
        self.assertEqual(llm.calls[0][0].content, llm.calls[1][0].content)  # identical system prompt

    def test_context_is_used_for_this_run_only(self) -> None:
        llm = RecordingLLM({"content": "ok"}, {"content": "ok"})
        agent = Agent(_config(), SkillLoader(LIBRARY), llm=llm)  # type: ignore[arg-type]

        agent.run("interpret this", context={"observation": {"temperature": 21}})
        agent.run("and now?")

        first = str(llm.calls[0][1].content)
        second = "\n".join(str(m.content) for m in llm.calls[1])
        self.assertIn("temperature", first)
        self.assertNotIn("temperature", second)

    def test_system_prompt_carries_no_memory_sections(self) -> None:
        llm = RecordingLLM({"content": "ok"})
        agent = Agent(_config(), SkillLoader(LIBRARY), llm=llm)  # type: ignore[arg-type]

        agent.run("hello")

        system = str(llm.calls[0][0].content)
        for banned in ("Global Memory", "Workspace Memory", "User Profile", "Workspace Profile"):
            self.assertNotIn(banned, system)

    def test_confirmation_approval_does_not_persist_across_runs(self) -> None:
        prompts: list[str] = []

        class NeedsOk:
            name = "needs_ok"
            description = "Needs confirmation."
            requires_confirmation = True
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}

            def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
                return ToolResult(True, "done")

        registry = ToolRegistry()
        registry.register(NeedsOk())

        def confirm(name: str, _args: dict) -> bool:
            prompts.append(name)
            return True

        class AlwaysCalls:
            def complete(self, model: str, messages: list[Message], tools: ToolRegistry) -> dict[str, object]:
                if any(message.role == "tool" for message in messages):
                    return {"content": "done", "tool_calls": []}
                return {"content": "", "tool_calls": [{"id": "c1", "name": "needs_ok", "arguments": {}}]}

        agent = Agent(
            _config(require_confirmation=True),
            SkillLoader(LIBRARY),
            tools=registry,
            llm=AlwaysCalls(),  # type: ignore[arg-type]
            confirm=confirm,
        )

        agent.run("one")
        agent.run("two")

        self.assertEqual(prompts, ["needs_ok", "needs_ok"])


class StructuredOutputTests(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {
            "state_deltas": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["state_deltas", "confidence"],
        "additionalProperties": False,
    }

    def _agent(self, reply: str) -> tuple[Agent, RecordingLLM]:
        llm = RecordingLLM({"content": reply})
        return Agent(_config(), SkillLoader(LIBRARY), llm=llm), llm  # type: ignore[arg-type]

    def test_valid_json_is_returned_as_data(self) -> None:
        agent, llm = self._agent('{"state_deltas": ["a"], "confidence": 0.91}')

        result = agent.run("interpret", output_schema=self.SCHEMA)

        self.assertEqual(result.data, {"state_deltas": ["a"], "confidence": 0.91})
        self.assertEqual(json.loads(result.text), result.data)
        # The schema is put in front of the model as well as validated afterwards.
        self.assertIn("state_deltas", str(llm.calls[0][0].content))

    def test_fenced_json_is_accepted(self) -> None:
        agent, _ = self._agent('```json\n{"state_deltas": [], "confidence": 0.5}\n```')

        result = agent.run("interpret", output_schema=self.SCHEMA)

        self.assertEqual(result.data, {"state_deltas": [], "confidence": 0.5})

    def test_malformed_json_is_rejected_not_repaired(self) -> None:
        agent, _ = self._agent('{"state_deltas": ["a", "confidence": 0.91')

        with self.assertRaises(StructuredOutputError) as caught:
            agent.run("interpret", output_schema=self.SCHEMA)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_prose_answer_is_rejected(self) -> None:
        agent, _ = self._agent("Sure! The confidence is about 0.9.")

        with self.assertRaises(StructuredOutputError):
            agent.run("interpret", output_schema=self.SCHEMA)

    def test_schema_violation_is_rejected(self) -> None:
        agent, _ = self._agent('{"state_deltas": ["a"]}')

        with self.assertRaises(StructuredOutputError) as caught:
            agent.run("interpret", output_schema=self.SCHEMA)
        self.assertIn("confidence", str(caught.exception))

    def test_wrong_type_is_rejected(self) -> None:
        agent, _ = self._agent('{"state_deltas": "not-a-list", "confidence": 0.5}')

        with self.assertRaises(StructuredOutputError) as caught:
            agent.run("interpret", output_schema=self.SCHEMA)
        self.assertIn("expected array", str(caught.exception))

    def test_no_schema_returns_plain_text(self) -> None:
        agent, llm = self._agent("just prose")

        result = agent.run("say something")

        self.assertEqual(result.text, "just prose")
        self.assertIsNone(result.data)
        self.assertNotIn("Required output format", str(llm.calls[0][0].content))


if __name__ == "__main__":
    unittest.main()
