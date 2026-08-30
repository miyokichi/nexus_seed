"""End-to-end tests for A2A: the protocol surface, structured parts, delegation.

These drive a **real** A2A server over HTTP with the real client, so the Agent
Card, JSON-RPC envelope, and task lifecycle are all exercised for real.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent import agents
from little_agent.config import builtin_skills_dir
from little_agent.a2a import models
from little_agent.agent import RunResult
from little_agent.a2a.client import A2AClient, A2AClientError, fetch_agent_card
from little_agent.a2a.grant import GrantPolicy, WorkGrant
from little_agent.a2a.peers import PeerPool, parse_peers
from little_agent.a2a.server import A2AService, serve
from little_agent.config import AgentConfig
from little_agent.factory import build_agent
from little_agent.control import StopController
from little_agent.tools.base import ToolContext
from little_agent.tools.delegation import DelegateTasksTool, DelegateTaskTool

LIBRARY = builtin_skills_dir()


def write_profile(agents_dir: Path, name: str, **profile) -> None:
    """Create an agents/<name>/agent.json, the way an operator would."""

    directory = agents_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.json").write_text(
        json.dumps({"name": name, **profile}), encoding="utf-8"
    )


class ScriptedAgent:
    """Stands in for a real Agent inside the A2A server."""

    def __init__(
        self,
        reply: str = "done",
        block: threading.Event | None = None,
        data: object = None,
    ) -> None:
        self.reply = reply
        self.block = block
        self.data = data
        self.prompts: list[str] = []
        self.contexts: list[dict | None] = []
        self.schemas: list[dict | None] = []
        self.depth: int | None = None
        self.grant = None

    def run(self, instruction, context=None, output_schema=None) -> RunResult:
        self.prompts.append(instruction)
        self.contexts.append(context)
        self.schemas.append(output_schema)
        if self.block is not None:
            self.block.wait(5)
        return RunResult(self.reply, data=self.data)


def _config(
    workspace: Path,
    agents_dir: Path,
    max_depth: int = 2,
    writable: tuple[Path, ...] = (),
    readable: tuple[Path, ...] = (),
) -> AgentConfig:
    return AgentConfig(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
        skill_library_dir=LIBRARY,
        agents_dir=agents_dir.resolve(),
        max_delegation_depth=max_depth,
        writable_paths=tuple(path.resolve() for path in writable),
        readable_paths=tuple(path.resolve() for path in readable),
    )


def _card(port: int, name: str = "test-agent", requires_auth: bool = False) -> dict:
    return models.agent_card(
        name=name,
        description="test",
        url=f"http://127.0.0.1:{port}/",
        version="0.1.0",
        skills=[models.agent_skill("general", "general", "general help")],
        requires_auth=requires_auth,
    )


class ServedAgent:
    """Context manager running an A2AService on a real loopback port."""

    def __init__(
        self,
        agent,
        token: str | None = None,
        grace: float = 2.0,
        grant_policy: GrantPolicy | None = None,
    ) -> None:
        self._agent = agent
        self._token = token
        self._grace = grace
        self._grant_policy = grant_policy
        self.depths: list[int] = []
        self.grants: list[WorkGrant] = []

    def __enter__(self) -> "ServedAgent":
        def factory(depth, stop, grant):
            self.depths.append(depth)
            self.grants.append(grant)
            self._agent.depth = depth
            self._agent.stop = stop
            self._agent.grant = grant
            return self._agent

        self.service = A2AService(
            _card(0, requires_auth=bool(self._token)),
            factory,
            token=self._token,
            grace_seconds=self._grace,
            grant_policy=self._grant_policy,
        )
        # Bind port 0, then publish the real port in the card the client reads.
        self.httpd = serve(self.service, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/"
        self.service.card["url"] = self.base_url
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class AgentCardTests(unittest.TestCase):
    def test_card_served_at_canonical_and_legacy_paths(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            for path in (models.AGENT_CARD_PATH, models.LEGACY_AGENT_CARD_PATH):
                with urllib.request.urlopen(served.base_url.rstrip("/") + path, timeout=5) as resp:
                    card = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(card["protocolVersion"], models.PROTOCOL_VERSION)
                self.assertEqual(card["preferredTransport"], "JSONRPC")
                self.assertIn("skills", card)
                # Streaming is genuinely unimplemented, so it must not be advertised.
                self.assertFalse(card["capabilities"]["streaming"])

    def test_client_discovers_card(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            card = fetch_agent_card(served.base_url)
            self.assertEqual(card["name"], "test-agent")


class MessageSendTests(unittest.TestCase):
    def test_task_completes_and_returns_artifact(self) -> None:
        agent = ScriptedAgent("the answer is 42")
        with ServedAgent(agent) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("what is the answer?")

            self.assertEqual(task["kind"], "task")
            self.assertEqual(task["status"]["state"], models.TASK_COMPLETED)
            self.assertEqual(models.task_result_text(task), "the answer is 42")
            self.assertEqual(agent.prompts, ["what is the answer?"])

    def test_depth_travels_in_message_metadata(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            client = A2AClient.connect(served.base_url)
            client.run_task("go", depth=2)
            self.assertEqual(served.depths, [2])

    def test_tasks_get_returns_the_same_task(self) -> None:
        with ServedAgent(ScriptedAgent("ok")) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("go")
            fetched = client.get_task(task["id"])
            self.assertEqual(fetched["id"], task["id"])
            self.assertEqual(fetched["status"]["state"], models.TASK_COMPLETED)

    def test_agent_failure_becomes_failed_task(self) -> None:
        class Boom:
            def run(self, _instruction, context=None, output_schema=None):
                raise RuntimeError("kaboom")

        with ServedAgent(Boom()) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("go")
            self.assertEqual(task["status"]["state"], models.TASK_FAILED)
            self.assertIn("kaboom", models.task_result_text(task))

    def test_long_task_returns_non_terminal_then_polls_to_completion(self) -> None:
        gate = threading.Event()
        agent = ScriptedAgent("slow result", block=gate)
        # Grace of 0 forces message/send to hand back a working task to poll.
        with ServedAgent(agent, grace=0.0) as served:
            client = A2AClient.connect(served.base_url)
            first = client.send_message("go")
            self.assertIn(first["status"]["state"], {models.TASK_SUBMITTED, models.TASK_WORKING})
            gate.set()
            task = client.run_task("go2")
            self.assertEqual(task["status"]["state"], models.TASK_COMPLETED)


class CancelTests(unittest.TestCase):
    def test_cancel_marks_task_canceled_and_trips_stop(self) -> None:
        gate = threading.Event()
        agent = ScriptedAgent("never used", block=gate)
        with ServedAgent(agent, grace=0.0) as served:
            client = A2AClient.connect(served.base_url)
            task = client.send_message("long job")
            canceled = client.cancel_task(task["id"])

            self.assertEqual(canceled["status"]["state"], models.TASK_CANCELED)
            # The served agent's stop controller is tripped, which is what aborts
            # a real Agent between tool calls.
            self.assertTrue(agent.stop.triggered)
            gate.set()

    def test_cancel_unknown_task_returns_task_not_found(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            client = A2AClient.connect(served.base_url)
            with self.assertRaises(A2AClientError) as caught:
                client.cancel_task("does-not-exist")
            self.assertIn(str(models.TASK_NOT_FOUND), str(caught.exception))

    def test_cancel_completed_task_is_rejected(self) -> None:
        with ServedAgent(ScriptedAgent("fast")) as served:
            client = A2AClient.connect(served.base_url)
            task = client.run_task("go")
            with self.assertRaises(A2AClientError) as caught:
                client.cancel_task(task["id"])
            self.assertIn(str(models.TASK_NOT_CANCELABLE), str(caught.exception))


class ProtocolErrorTests(unittest.TestCase):
    def _post(self, base_url: str, payload: dict) -> dict:
        request = urllib.request.Request(
            base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_unknown_method(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(
                served.base_url, {"jsonrpc": "2.0", "id": 1, "method": "nope", "params": {}}
            )
            self.assertEqual(body["error"]["code"], models.METHOD_NOT_FOUND)
            self.assertEqual(body["id"], 1)

    def test_streaming_reports_unsupported_operation(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(
                served.base_url,
                {"jsonrpc": "2.0", "id": 2, "method": "message/stream", "params": {}},
            )
            self.assertEqual(body["error"]["code"], models.UNSUPPORTED_OPERATION)

    def test_wrong_jsonrpc_version(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(served.base_url, {"jsonrpc": "1.0", "id": 3, "method": "tasks/get"})
            self.assertEqual(body["error"]["code"], models.INVALID_REQUEST)

    def test_message_without_text_part_is_rejected(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            body = self._post(
                served.base_url,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "message/send",
                    "params": {"message": {"kind": "message", "role": "user", "parts": []}},
                },
            )
            self.assertEqual(body["error"]["code"], models.CONTENT_TYPE_NOT_SUPPORTED)


class PartsTests(unittest.TestCase):
    """Requests and results carry text, structured data, or both."""

    def test_text_part_request_and_text_part_response(self) -> None:
        agent = ScriptedAgent("plain answer")
        with ServedAgent(agent) as served:
            task = A2AClient.connect(served.base_url).run_task("say something")

            self.assertEqual(agent.prompts, ["say something"])
            self.assertEqual(agent.contexts, [None])
            parts = task["artifacts"][0]["parts"]
            self.assertEqual([part["kind"] for part in parts], ["text"])
            self.assertEqual(models.task_result_text(task), "plain answer")
            self.assertIsNone(models.task_result_data(task))

    def test_data_part_request_carries_instruction_context_and_schema(self) -> None:
        agent = ScriptedAgent("ok")
        with ServedAgent(agent) as served:
            A2AClient.connect(served.base_url).run_task(
                data={
                    "instruction": "Interpret this observation",
                    "context": {"observation": {"temp": 21}, "world_state": {"door": "open"}},
                    "output_schema": {"type": "object"},
                }
            )

            self.assertEqual(agent.prompts, ["Interpret this observation"])
            self.assertEqual(
                agent.contexts[0], {"observation": {"temp": 21}, "world_state": {"door": "open"}}
            )
            self.assertEqual(agent.schemas[0], {"type": "object"})

    def test_bare_data_part_becomes_context(self) -> None:
        agent = ScriptedAgent("ok")
        with ServedAgent(agent) as served:
            A2AClient.connect(served.base_url).run_task(
                "summarize the reading", data={"observation": {"temp": 21}}
            )

            self.assertEqual(agent.prompts, ["summarize the reading"])
            self.assertEqual(agent.contexts[0], {"observation": {"temp": 21}})

    def test_data_part_response_round_trips(self) -> None:
        agent = ScriptedAgent('{"confidence": 0.91}', data={"confidence": 0.91})
        with ServedAgent(agent) as served:
            task = A2AClient.connect(served.base_url).run_task(
                data={"instruction": "score it", "output_schema": {"type": "object"}}
            )

            parts = task["artifacts"][0]["parts"]
            self.assertEqual([part["kind"] for part in parts], ["data"])
            self.assertEqual(models.task_result_data(task), {"confidence": 0.91})
            # Text extraction still works for text-only consumers (e.g. delegation).
            self.assertEqual(json.loads(models.task_result_text(task)), {"confidence": 0.91})

    def test_message_with_neither_text_nor_instruction_is_rejected(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            request = urllib.request.Request(
                served.base_url,
                data=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "message/send",
                        "params": {
                            "message": {
                                "kind": "message",
                                "role": "user",
                                "parts": [{"kind": "data", "data": {"context": {"a": 1}}}],
                            }
                        },
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(body["error"]["code"], models.CONTENT_TYPE_NOT_SUPPORTED)

    def test_structured_output_failure_becomes_a_failed_task(self) -> None:
        class BadJSON:
            def run(self, _instruction, context=None, output_schema=None):
                from little_agent.agent import StructuredOutputError

                raise StructuredOutputError("not valid JSON")

        with ServedAgent(BadJSON()) as served:
            task = A2AClient.connect(served.base_url).run_task(
                data={"instruction": "x", "output_schema": {"type": "object"}}
            )

            self.assertEqual(task["status"]["state"], models.TASK_FAILED)
            self.assertIn("not valid JSON", models.task_result_text(task))

    def test_card_advertises_both_input_and_output_modes(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            card = fetch_agent_card(served.base_url)
            self.assertIn("application/json", card["defaultInputModes"])
            self.assertIn("application/json", card["defaultOutputModes"])


class ParseRequestPartsTests(unittest.TestCase):
    def test_text_and_data_instructions_are_combined(self) -> None:
        payload = models.parse_request_parts(
            [
                models.text_part("first"),
                models.data_part({"instruction": "second", "context": {"a": 1}}),
            ]
        )
        self.assertEqual(payload.instruction, "first\n\nsecond")
        self.assertEqual(payload.context, {"a": 1})

    def test_several_bare_data_parts_merge_into_context(self) -> None:
        payload = models.parse_request_parts(
            [models.text_part("go"), models.data_part({"a": 1}), models.data_part({"b": 2})]
        )
        self.assertEqual(payload.context, {"a": 1, "b": 2})

    def test_non_object_data_part_is_kept_as_context_data(self) -> None:
        payload = models.parse_request_parts([models.text_part("go"), models.data_part([1, 2])])
        self.assertEqual(payload.context, {"data": [[1, 2]]})

    def test_no_parts_yields_an_empty_instruction(self) -> None:
        self.assertEqual(models.parse_request_parts(None).instruction, "")
        self.assertEqual(models.parse_request_parts([]).instruction, "")


class AuthTests(unittest.TestCase):
    def test_token_required_when_configured(self) -> None:
        with ServedAgent(ScriptedAgent(), token="s3cret") as served:
            # The card stays public so peers can learn how to authenticate.
            card = fetch_agent_card(served.base_url)
            self.assertIn("securitySchemes", card)

            with self.assertRaises(A2AClientError):
                A2AClient(card).run_task("go")

            authorized = A2AClient(card, token="s3cret")
            self.assertEqual(authorized.run_task("go")["status"]["state"], models.TASK_COMPLETED)


class DelegateToolTests(unittest.TestCase):
    def test_delegates_over_a2a_to_a_url(self) -> None:
        agent = ScriptedAgent("peer finished the research")
        with TemporaryDirectory() as tmp, ServedAgent(agent) as served:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, depth=0, pool=PeerPool(config))
            result = tool.run(
                ToolContext(root), task="research X", background="use Y", agent_url=served.base_url
            )

            self.assertTrue(result.ok, result.content)
            self.assertIn("peer finished the research", result.content)
            self.assertIn("research X", agent.prompts[0])
            self.assertIn("use Y", agent.prompts[0])
            # The parent's depth+1 is what the peer is told to run at.
            self.assertEqual(served.depths, [1])

    def test_empty_task_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, pool=PeerPool(config))
            self.assertFalse(tool.run(ToolContext(root), task="  ").ok)

    def test_depth_limit_blocks_further_delegation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=2)
            tool = DelegateTaskTool(config=config, depth=2, pool=PeerPool(config))
            result = tool.run(ToolContext(root), task="anything")
            self.assertFalse(result.ok)
            self.assertIn("depth limit", result.content)

    def test_unreachable_peer_reports_cleanly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, pool=PeerPool(config))
            result = tool.run(
                ToolContext(root), task="t", agent_url="http://127.0.0.1:9/"
            )
            self.assertFalse(result.ok)
            self.assertIn("peer", result.content.lower())

    def test_failed_peer_task_surfaces_as_tool_error(self) -> None:
        class Boom:
            def run(self, _instruction, context=None, output_schema=None):
                raise RuntimeError("peer exploded")

        with TemporaryDirectory() as tmp, ServedAgent(Boom()) as served:
            root = Path(tmp)
            config = _config(root, root / "agents")
            tool = DelegateTaskTool(config=config, pool=PeerPool(config))
            result = tool.run(ToolContext(root), task="t", agent_url=served.base_url)
            self.assertFalse(result.ok)
            self.assertIn("peer exploded", result.content)


class SlowAgent:
    """Sleeps for a fixed time so concurrency is observable in wall-clock terms."""

    def __init__(self, delay: float, reply: str = "ok") -> None:
        self.delay = delay
        self.reply = reply
        self.concurrent = 0
        self.peak = 0
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def run(self, instruction, context=None, output_schema=None) -> RunResult:
        with self._lock:
            self.prompts.append(instruction)
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            time.sleep(self.delay)
            return RunResult(f"{self.reply}:{instruction}")
        finally:
            with self._lock:
                self.concurrent -= 1


class ParallelDelegationTests(unittest.TestCase):
    def _tool(self, root: Path, max_parallel: int = 4, stop=None) -> DelegateTasksTool:
        config = _config(root, root / "agents")
        config = replace(config, max_parallel_delegations=max_parallel)
        return DelegateTasksTool(config=config, pool=PeerPool(config), stop=stop)

    def test_subtasks_run_concurrently(self) -> None:
        agent = SlowAgent(delay=0.6)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp))
            started = time.monotonic()
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "alpha", "agent_url": served.base_url},
                    {"task": "bravo", "agent_url": served.base_url},
                    {"task": "charlie", "agent_url": served.base_url},
                ],
            )
            elapsed = time.monotonic() - started

            self.assertTrue(result.ok, result.content)
            # Three 0.6s tasks would take ~1.8s sequentially; in parallel they overlap.
            self.assertLess(elapsed, 1.5, f"took {elapsed:.2f}s — subtasks did not overlap")
            self.assertGreater(agent.peak, 1, "no two subtasks were ever in flight together")
            for name in ("alpha", "bravo", "charlie"):
                self.assertIn(name, result.content)

    def test_results_keep_request_order(self) -> None:
        # Later subtasks finish first, but output order must match the request.
        agent = SlowAgent(delay=0.0)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp))
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "first", "agent_url": served.base_url},
                    {"task": "second", "agent_url": served.base_url},
                ],
            )
            self.assertLess(result.content.index("first"), result.content.index("second"))
            self.assertIn("[1/2]", result.content)
            self.assertIn("[2/2]", result.content)

    def test_concurrency_is_capped(self) -> None:
        agent = SlowAgent(delay=0.4)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp), max_parallel=2)
            tool.run(
                ToolContext(Path(tmp)),
                tasks=[{"task": f"t{i}", "agent_url": served.base_url} for i in range(5)],
            )
            self.assertLessEqual(agent.peak, 2, f"peak concurrency was {agent.peak}, cap was 2")

    def test_partial_failure_keeps_good_results(self) -> None:
        agent = SlowAgent(delay=0.0)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp))
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "good", "agent_url": served.base_url},
                    {"task": "bad", "agent_url": "http://127.0.0.1:9/"},
                ],
            )
            # One peer is unreachable, but the successful result must survive.
            self.assertTrue(result.ok, result.content)
            self.assertIn("1 completed, 1 failed", result.content)
            self.assertIn("good", result.content)
            self.assertIn("FAILED", result.content)

    def test_all_failed_is_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp))
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[
                    {"task": "a", "agent_url": "http://127.0.0.1:9/"},
                    {"task": "b", "agent_url": "http://127.0.0.1:9/"},
                ],
            )
            self.assertFalse(result.ok)
            self.assertIn("0 completed, 2 failed", result.content)

    def test_empty_and_malformed_input_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp))
            self.assertFalse(tool.run(ToolContext(Path(tmp)), tasks=[]).ok)
            self.assertFalse(tool.run(ToolContext(Path(tmp)), tasks=["not an object"]).ok)

    def test_depth_limit_blocks_parallel_delegation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=2)
            tool = DelegateTasksTool(config=config, depth=2, pool=PeerPool(config))
            result = tool.run(ToolContext(root), tasks=[{"task": "x"}])
            self.assertFalse(result.ok)
            self.assertIn("depth limit", result.content)

    def test_stop_abandons_delegation_and_cancels_peer(self) -> None:
        stop = StopController("<ctrl>+<alt>+q")
        agent = SlowAgent(delay=3.0)
        with TemporaryDirectory() as tmp, ServedAgent(agent, grace=0.0) as served:
            tool = self._tool(Path(tmp), stop=stop)

            # Trip the emergency stop shortly after the delegation starts.
            threading.Timer(0.4, stop._on_activate).start()
            started = time.monotonic()
            result = tool.run(
                ToolContext(Path(tmp)),
                tasks=[{"task": "long", "agent_url": served.base_url}],
            )
            elapsed = time.monotonic() - started

            self.assertFalse(result.ok)
            self.assertIn("Stopped", result.content)
            # Returned well before the peer's 3s task would have finished.
            self.assertLess(elapsed, 2.5, f"took {elapsed:.2f}s — stop was not honored")
            # The abandoned peer task was cancelled rather than left running.
            self.assertTrue(agent.stop.triggered)


class TaskIsolationTests(unittest.TestCase):
    """Successive A2A tasks must not see each other's conversation."""

    def test_each_task_gets_its_own_agent_and_context(self) -> None:
        built: list[object] = []

        def factory(depth, stop, grant):
            agent = ScriptedAgent(f"answer {len(built) + 1}")
            built.append(agent)
            return agent

        service = A2AService(_card(0), factory, grace_seconds=2.0)
        httpd = serve(service, "127.0.0.1", 0)
        port = httpd.server_address[1]
        service.card["url"] = f"http://127.0.0.1:{port}/"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            client = A2AClient.connect(service.card["url"])
            first = client.run_task("remember the number 42", timeout=10)
            second = client.run_task("what number did I say?", timeout=10)
        finally:
            httpd.shutdown()
            httpd.server_close()

        self.assertEqual(models.task_result_text(first), "answer 1")
        self.assertEqual(models.task_result_text(second), "answer 2")
        # Two separate agents, and neither saw the other's prompt.
        self.assertEqual(len(built), 2)
        self.assertEqual(built[0].prompts, ["remember the number 42"])
        self.assertEqual(built[1].prompts, ["what number did I say?"])
        self.assertNotEqual(first["contextId"], second["contextId"])

    def test_a_real_served_agent_starts_from_a_clean_transcript(self) -> None:
        """The runtime itself: two tasks through one profile share no history."""

        seen: list[list[str]] = []

        class Recorder:
            def complete(self, model, messages, tools):
                seen.append([message.role for message in messages])
                return {"content": "done", "tool_calls": []}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            profile = agents.default_profile(config)

            def factory(depth, stop, grant):
                agent = build_agent(config, profile, lambda *_: True, stop, depth=depth)
                agent.llm = Recorder()
                return agent

            service = A2AService(_card(0), factory, grace_seconds=5.0)
            httpd = serve(service, "127.0.0.1", 0)
            port = httpd.server_address[1]
            service.card["url"] = f"http://127.0.0.1:{port}/"
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                client = A2AClient.connect(service.card["url"])
                client.run_task("first", timeout=10)
                client.run_task("second", timeout=10)
            finally:
                httpd.shutdown()
                httpd.server_close()

        self.assertEqual(seen, [["system", "user"], ["system", "user"]])

    def test_a_served_agent_persists_no_memory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x")
            )
            self.assertFalse(agent.memory.enabled)
            self.assertNotIn("update_workspace_memory", agent.tools.names())
            self.assertNotIn("update_global_memory", agent.tools.names())


class PushNotificationTests(unittest.TestCase):
    """Push config gets its own error code, not the generic one."""

    def _post(self, base_url: str, method: str) -> dict:
        request = urllib.request.Request(
            base_url,
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 9, "method": method, "params": {}}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_every_push_config_method_reports_push_not_supported(self) -> None:
        methods = (
            "tasks/pushNotificationConfig/set",
            "tasks/pushNotificationConfig/get",
            "tasks/pushNotificationConfig/list",
            "tasks/pushNotificationConfig/delete",
        )
        with ServedAgent(ScriptedAgent()) as served:
            for method in methods:
                body = self._post(served.base_url, method)
                self.assertEqual(
                    body["error"]["code"],
                    models.PUSH_NOTIFICATION_NOT_SUPPORTED,
                    f"unexpected error for {method}",
                )

    def test_card_declares_push_unsupported(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            card = fetch_agent_card(served.base_url)
            self.assertFalse(card["capabilities"]["pushNotifications"])


class WorkGrantOverA2ATests(unittest.TestCase):
    """workspace / allowed_paths across the wire, and the server-side gate."""

    def test_grant_reaches_the_server_and_builds_the_task_agent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            case = root / "case-7"
            case.mkdir()
            policy = GrantPolicy(writable_roots=(root,))
            with ServedAgent(ScriptedAgent(), grant_policy=policy) as served:
                client = A2AClient.connect(served.base_url)
                task = client.run_task(
                    "do the work",
                    timeout=10,
                    grant=WorkGrant(workspace=case, allowed_paths=(root / "prices.xlsx",)),
                )

            self.assertEqual(task["status"]["state"], models.TASK_COMPLETED)
            self.assertEqual(served.grants[0].workspace, case)
            self.assertEqual(served.grants[0].allowed_paths, (root / "prices.xlsx",))

    def test_a_server_without_a_policy_refuses_any_grant(self) -> None:
        with TemporaryDirectory() as tmp:
            with ServedAgent(ScriptedAgent()) as served:
                client = A2AClient.connect(served.base_url)
                with self.assertRaises(A2AClientError) as caught:
                    client.run_task("do it", timeout=10, grant=WorkGrant(workspace=Path(tmp)))
            self.assertIn(str(models.INVALID_PARAMS), str(caught.exception))

    def test_a_server_refuses_a_path_it_cannot_reach(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as outside:
            policy = GrantPolicy(writable_roots=(Path(tmp).resolve(),))
            with ServedAgent(ScriptedAgent(), grant_policy=policy) as served:
                client = A2AClient.connect(served.base_url)
                with self.assertRaises(A2AClientError) as caught:
                    client.run_task(
                        "do it", timeout=10, grant=WorkGrant(workspace=Path(outside).resolve())
                    )
            message = str(caught.exception)
            self.assertIn(str(models.INVALID_PARAMS), message)
            self.assertIn("outside the writable paths", message)

    def test_a_readable_only_path_is_grantable_as_an_allowed_path(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as reference:
            root = Path(tmp).resolve()
            reference_root = Path(reference).resolve()
            policy = GrantPolicy(writable_roots=(root,), readable_roots=(reference_root,))
            with ServedAgent(ScriptedAgent(), grant_policy=policy) as served:
                client = A2AClient.connect(served.base_url)
                task = client.run_task(
                    "read the reference",
                    timeout=10,
                    grant=WorkGrant(
                        workspace=root, allowed_paths=(reference_root / "notes.md",)
                    ),
                )
            self.assertEqual(task["status"]["state"], models.TASK_COMPLETED)
            self.assertEqual(served.grants[0].allowed_paths, (reference_root / "notes.md",))

    def test_a_readable_only_path_is_refused_as_a_workspace(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as reference:
            reference_root = Path(reference).resolve()
            policy = GrantPolicy(
                writable_roots=(Path(tmp).resolve(),), readable_roots=(reference_root,)
            )
            with ServedAgent(ScriptedAgent(), grant_policy=policy) as served:
                client = A2AClient.connect(served.base_url)
                with self.assertRaises(A2AClientError) as caught:
                    client.run_task(
                        "work there", timeout=10, grant=WorkGrant(workspace=reference_root)
                    )
            self.assertIn("outside the writable paths", str(caught.exception))

    def test_a_granted_allowed_path_is_readable_but_not_writable(self) -> None:
        """End to end: the peer can read the reference, and cannot overwrite it."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / "server-home"
            case = root / "case-7"
            shared = root / "shared"
            for directory in (home, case, shared):
                directory.mkdir()
            (shared / "prices.txt").write_text("100 yen", encoding="utf-8")
            config = _config(home, root / "agents")
            profile = agents.default_profile(config)
            outcomes: list[str] = []

            class ReadThenWriteClient:
                """Reads the granted reference, then tries to overwrite it."""

                def __init__(self) -> None:
                    self.step = 0

                def complete(self, model, messages, tools):
                    for message in messages:
                        if message.role == "tool":
                            outcomes.append(str(message.content))
                    self.step += 1
                    if self.step == 1:
                        return {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "name": "read_file",
                                    "arguments": {"path": str(shared / "prices.txt")},
                                }
                            ],
                        }
                    if self.step == 2:
                        return {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c2",
                                    "name": "write_file",
                                    "arguments": {
                                        "path": str(shared / "prices.txt"),
                                        "content": "999 yen",
                                    },
                                }
                            ],
                        }
                    return {"content": "done", "tool_calls": []}

            def factory(depth, stop, grant):
                agent = build_agent(
                    grant.apply(config), profile, lambda *_: True, stop, depth=depth
                )
                agent.llm = ReadThenWriteClient()
                return agent

            service = A2AService(
                _card(0),
                factory,
                grace_seconds=5.0,
                grant_policy=GrantPolicy(writable_roots=(root,)),
            )
            httpd = serve(service, "127.0.0.1", 0)
            port = httpd.server_address[1]
            service.card["url"] = f"http://127.0.0.1:{port}/"
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                client = A2AClient.connect(service.card["url"])
                client.run_task(
                    "look and then meddle",
                    timeout=15,
                    grant=WorkGrant(workspace=case, allowed_paths=(shared,)),
                )
            finally:
                httpd.shutdown()
                httpd.server_close()

            joined = " ".join(outcomes)
            self.assertIn("100 yen", joined)
            self.assertIn("outside allowed write paths", joined)
            # The reference survived untouched.
            self.assertEqual(
                (shared / "prices.txt").read_text(encoding="utf-8"), "100 yen"
            )

    def test_an_ungranted_task_runs_in_the_servers_own_workspace(self) -> None:
        with ServedAgent(ScriptedAgent()) as served:
            client = A2AClient.connect(served.base_url)
            client.run_task("plain task", timeout=10)
        self.assertTrue(served.grants[0].is_empty)

    def test_a_served_agent_really_works_in_the_granted_directory(self) -> None:
        """End to end: the grant decides where a relative write_file lands."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / "server-home"
            case = root / "case-7"
            home.mkdir()
            case.mkdir()
            config = _config(home, root / "agents")
            profile = agents.default_profile(config)

            class WritingClient:
                """Calls write_file once, then answers."""

                def __init__(self) -> None:
                    self.done = False

                def complete(self, model, messages, tools):
                    if self.done:
                        return {"content": "written", "tool_calls": []}
                    self.done = True
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "name": "write_file",
                                "arguments": {"path": "report.txt", "content": "hello"},
                            }
                        ],
                    }

            def factory(depth, stop, grant):
                agent = build_agent(
                    grant.apply(config), profile, lambda *_: True, stop, depth=depth
                )
                agent.llm = WritingClient()
                return agent

            service = A2AService(
                _card(0), factory, grace_seconds=5.0, grant_policy=GrantPolicy(writable_roots=(root,))
            )
            httpd = serve(service, "127.0.0.1", 0)
            port = httpd.server_address[1]
            service.card["url"] = f"http://127.0.0.1:{port}/"
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                client = A2AClient.connect(service.card["url"])
                task = client.run_task(
                    "write the report", timeout=15, grant=WorkGrant(workspace=case)
                )
            finally:
                httpd.shutdown()
                httpd.server_close()

            self.assertEqual(task["status"]["state"], models.TASK_COMPLETED)
            # The relative path resolved inside the granted directory, not the
            # server's own workspace.
            self.assertEqual((case / "report.txt").read_text(encoding="utf-8"), "hello")
            self.assertFalse((home / "report.txt").exists())


class DelegationGrantTests(unittest.TestCase):
    """The caller-side gate: you can only hand over what you can write."""

    def test_delegate_task_sends_a_grant_it_may_hand_over(self) -> None:
        agent = ScriptedAgent("done")
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "case-7").mkdir()
            policy = GrantPolicy(writable_roots=(root,))
            with ServedAgent(agent, grant_policy=policy) as served:
                config = _config(root, root / "agents")
                tool = DelegateTaskTool(config=config, depth=0, pool=PeerPool(config))
                result = tool.run(
                    ToolContext(root),
                    task="finish the draft",
                    agent_url=served.base_url,
                    workspace="case-7",
                )

            self.assertTrue(result.ok, result.content)
            self.assertEqual(served.grants[0].workspace, root / "case-7")

    def test_a_path_the_caller_cannot_reach_is_refused_before_sending(self) -> None:
        agent = ScriptedAgent("done")
        with TemporaryDirectory() as tmp, TemporaryDirectory() as outside:
            root = Path(tmp).resolve()
            with ServedAgent(agent, grant_policy=GrantPolicy(allow_any=True)) as served:
                config = _config(root, root / "agents")
                tool = DelegateTaskTool(config=config, depth=0, pool=PeerPool(config))
                result = tool.run(
                    ToolContext(root),
                    task="finish the draft",
                    agent_url=served.base_url,
                    allowed_paths=[str(Path(outside).resolve())],
                )

            self.assertFalse(result.ok)
            self.assertIn("Cannot hand over that work directory", result.content)
            # Nothing was sent: the peer never heard about the subtask.
            self.assertEqual(agent.prompts, [])

    def test_a_readable_only_path_can_be_handed_on_as_reference(self) -> None:
        """Read-only reach is enough for allowed_paths, which convey read only."""

        agent = ScriptedAgent("done")
        with TemporaryDirectory() as tmp, TemporaryDirectory() as reference:
            root = Path(tmp).resolve()
            reference_root = Path(reference).resolve()
            with ServedAgent(agent, grant_policy=GrantPolicy(allow_any=True)) as served:
                config = _config(root, root / "agents", readable=(reference_root,))
                tool = DelegateTaskTool(config=config, depth=0, pool=PeerPool(config))
                result = tool.run(
                    ToolContext(root),
                    task="consult the reference",
                    agent_url=served.base_url,
                    allowed_paths=[str(reference_root / "notes.md")],
                )

            self.assertTrue(result.ok, result.content)
            self.assertEqual(served.grants[0].allowed_paths, (reference_root / "notes.md",))

    def test_a_readable_only_path_cannot_be_handed_on_as_a_workspace(self) -> None:
        """A workspace is written in, so the caller must be able to write it."""

        agent = ScriptedAgent("done")
        with TemporaryDirectory() as tmp, TemporaryDirectory() as reference:
            root = Path(tmp).resolve()
            reference_root = Path(reference).resolve()
            with ServedAgent(agent, grant_policy=GrantPolicy(allow_any=True)) as served:
                config = _config(root, root / "agents", readable=(reference_root,))
                tool = DelegateTaskTool(config=config, depth=0, pool=PeerPool(config))
                result = tool.run(
                    ToolContext(root),
                    task="work in the reference folder",
                    agent_url=served.base_url,
                    workspace=str(reference_root),
                )

            self.assertFalse(result.ok)
            self.assertIn("outside the writable paths", result.content)
            self.assertEqual(agent.prompts, [])

    def test_a_configured_writable_path_may_be_handed_on(self) -> None:
        agent = ScriptedAgent("done")
        with TemporaryDirectory() as tmp, TemporaryDirectory() as shared:
            root = Path(tmp).resolve()
            shared_root = Path(shared).resolve()
            with ServedAgent(agent, grant_policy=GrantPolicy(allow_any=True)) as served:
                config = _config(root, root / "agents", writable=(shared_root,))
                tool = DelegateTaskTool(config=config, depth=0, pool=PeerPool(config))
                result = tool.run(
                    ToolContext(root),
                    task="use the price list",
                    agent_url=served.base_url,
                    allowed_paths=[str(shared_root / "prices.xlsx")],
                )

            self.assertTrue(result.ok, result.content)
            self.assertEqual(served.grants[0].allowed_paths, (shared_root / "prices.xlsx",))

    def test_parallel_subtasks_carry_their_own_grants(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ("case-a", "case-b"):
                (root / name).mkdir()
            policy = GrantPolicy(writable_roots=(root,))
            with ServedAgent(ScriptedAgent("done"), grant_policy=policy) as served:
                config = _config(root, root / "agents")
                tool = DelegateTasksTool(config=config, depth=0, pool=PeerPool(config))
                result = tool.run(
                    ToolContext(root),
                    tasks=[
                        {"task": "a", "agent_url": served.base_url, "workspace": "case-a"},
                        {"task": "b", "agent_url": served.base_url, "workspace": "case-b"},
                    ],
                )

            self.assertTrue(result.ok, result.content)
            granted = sorted(str(grant.workspace) for grant in served.grants)
            self.assertEqual(granted, [str(root / "case-a"), str(root / "case-b")])

    def test_one_refused_subtask_does_not_stop_the_others(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as outside:
            root = Path(tmp).resolve()
            (root / "case-a").mkdir()
            policy = GrantPolicy(writable_roots=(root,))
            with ServedAgent(ScriptedAgent("done"), grant_policy=policy) as served:
                config = _config(root, root / "agents")
                tool = DelegateTasksTool(config=config, depth=0, pool=PeerPool(config))
                result = tool.run(
                    ToolContext(root),
                    tasks=[
                        {"task": "a", "agent_url": served.base_url, "workspace": "case-a"},
                        {
                            "task": "b",
                            "agent_url": served.base_url,
                            "workspace": str(Path(outside).resolve()),
                        },
                    ],
                )

            self.assertTrue(result.ok, result.content)
            self.assertIn("1 completed, 1 failed", result.content)
            self.assertIn("Cannot hand over that work directory", result.content)
            self.assertEqual(len(served.grants), 1)


class PeerRegistryTests(unittest.TestCase):
    def test_parse_named_and_bare_urls(self) -> None:
        peers = parse_peers("office=http://127.0.0.1:8801/, http://example.com:9000/")
        self.assertEqual(peers["office"], "http://127.0.0.1:8801/")
        self.assertIn("example.com-9000", peers)

    def test_empty_input(self) -> None:
        self.assertEqual(parse_peers(None), {})
        self.assertEqual(parse_peers("  "), {})

    def test_available_includes_local_profiles(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            write_profile(agents_dir, "office", skills=["datetime"])
            pool = PeerPool(_config(root, agents_dir))
            self.assertIn("office", pool.available())
            self.assertIn(agents.DEFAULT_AGENT_NAME, pool.available())

    def test_unknown_local_profile_raises_before_spawning(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = PeerPool(_config(root, root / "agents"))
            with self.assertRaises(FileNotFoundError):
                pool.connect(name="does-not-exist")


class LocalSpawnTests(unittest.TestCase):
    """Spawning a real local A2A server subprocess for a profile.

    Only the spawn/discovery path is exercised (no task is sent), so the test
    never reaches an LLM even when the environment has API credentials.
    """

    def test_local_profile_is_served_and_discoverable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            write_profile(agents_dir, "helper", skills=["datetime"])
            pool = PeerPool(_config(root, agents_dir))
            try:
                client = pool.connect(name="helper")
                self.assertEqual(client.name, "little-agent/helper")
                # The profile's skills are advertised on the card.
                self.assertIn("datetime", [s["id"] for s in client.card["skills"]])
                # A second connect reuses the already-running server.
                self.assertEqual(pool.connect(name="helper").endpoint, client.endpoint)
            finally:
                pool.shutdown()

    def test_parallel_connects_start_exactly_one_server(self) -> None:
        """Concurrent delegations to one local profile must not race the spawn.

        Without per-name serialization a second caller could pick up the URL of a
        server that has not started listening yet, or start a duplicate.
        """

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            write_profile(agents_dir, "helper", skills=["datetime"])
            pool = PeerPool(_config(root, agents_dir))
            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    clients = list(
                        executor.map(lambda _: pool.connect(name="helper"), range(4))
                    )
                endpoints = {client.endpoint for client in clients}
                self.assertEqual(len(endpoints), 1, f"spawned more than one server: {endpoints}")
            finally:
                pool.shutdown()


    def test_a_spawned_child_accepts_a_grant_inside_the_parents_reach(self) -> None:
        """A locally spawned peer inherits the parent's reach, so a grant lands.

        No LLM is involved: the grant is authorized (or refused) before the
        child builds an agent, so an unauthorized path fails and an authorized
        one gets past the gate.
        """

        with TemporaryDirectory() as tmp, TemporaryDirectory() as outside:
            root = Path(tmp)
            agents_dir = root / "agents"
            (root / "case-7").mkdir()
            write_profile(agents_dir, "helper", skills=["datetime"])
            pool = PeerPool(_config(root, agents_dir))
            try:
                client = pool.connect(name="helper")

                # Inside the parent's workspace: the child authorizes it and
                # starts working (the LLM call then fails harmlessly).
                inside = client.send_message(
                    "noop", grant=WorkGrant(workspace=root / "case-7")
                )
                self.assertEqual(inside.get("kind"), "task")

                # Outside it: refused by the child's own policy, before any work.
                with self.assertRaises(A2AClientError) as caught:
                    client.send_message(
                        "noop", grant=WorkGrant(workspace=Path(outside).resolve())
                    )
                self.assertIn(str(models.INVALID_PARAMS), str(caught.exception))
            finally:
                pool.shutdown()


class BuildAgentDelegationTests(unittest.TestCase):
    def test_delegate_tool_registered_at_depth_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents")
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x")
            )
            self.assertIn("delegate_task", agent.tools.names())

    def test_delegate_tool_absent_at_depth_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=2)
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x"), depth=2
            )
            self.assertNotIn("delegate_task", agent.tools.names())

    def test_delegation_can_be_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, root / "agents", max_depth=0)
            agent = build_agent(
                config, agents.default_profile(config), lambda *_: True, StopController("x")
            )
            self.assertNotIn("delegate_task", agent.tools.names())


if __name__ == "__main__":
    unittest.main()
