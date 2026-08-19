"""Answering a question NEXUS SEED asked about itself, without a command.

Phase 6 writes what it does not know into ``self.unresolved_questions``; the
answer used to be reachable only through ``/answer``.  It is now an ordinary
event, so the reflection path is driven the same way from any channel.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.cockpit import CockpitService
from nexus_seed.processes.control import bootstrap_control
from nexus_seed.processes.persistent_being import bootstrap_persistent_being
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.questions import (
    QuestionAnswerError,
    answer_question,
    find_question,
    open_questions,
)
from nexus_seed.runtime.runtime import Runtime


pytestmark = pytest.mark.asyncio

TOKEN = "question-token"


def asking_runtime(tmp_path, name: str) -> Runtime:
    runtime = Runtime(tmp_path / name)
    bootstrap_semantic(runtime)
    bootstrap_control(runtime)
    bootstrap_persistent_being(runtime, enabled=True, wake_on_start=False)
    runtime.state_store.set(
        "self", "unresolved_questions", [{"question": "Which target is preferred?"}]
    )
    return runtime


async def test_answering_reaches_the_phase6_reflection_path(tmp_path):
    runtime = asking_runtime(tmp_path, "answer.db")
    try:
        question = open_questions(runtime)[0]
        event = await answer_question(
            runtime, question.question_id, "Use target A", actor="operator"
        )
        assert event is not None
        assert event.type == "self_question_answered"
        assert event.source == "human:operator"
        await runtime.run_pending()

        assert runtime.state_store.get("self", "unresolved_questions") == []
        beliefs = runtime.state_store.get("self", "beliefs")
        assert beliefs[-1]["answer"] == "Use target A"
        assert beliefs[-1]["claim_status"] == "CONFIRMED"
    finally:
        runtime.close()


async def test_an_already_answered_question_is_a_no_op(tmp_path):
    runtime = asking_runtime(tmp_path, "twice.db")
    try:
        question = open_questions(runtime)[0]
        assert await answer_question(runtime, question.question_id, "Use target A")
        await runtime.run_pending()
        events_before = len(runtime.event_store.all())
        assert await answer_question(runtime, question.question_id, "Use target A") is None
        assert len(runtime.event_store.all()) == events_before
    finally:
        runtime.close()


async def test_an_empty_answer_is_refused(tmp_path):
    runtime = asking_runtime(tmp_path, "empty.db")
    try:
        question = open_questions(runtime)[0]
        with pytest.raises(QuestionAnswerError):
            await answer_question(runtime, question.question_id, "   ")
        assert open_questions(runtime)
    finally:
        runtime.close()


async def test_an_unknown_question_is_not_an_error(tmp_path):
    runtime = asking_runtime(tmp_path, "unknown.db")
    try:
        assert find_question(runtime, "no-such-question") is None
        assert await answer_question(runtime, "no-such-question", "anything") is None
    finally:
        runtime.close()


async def test_the_cockpit_shows_the_id_the_endpoint_answers(tmp_path):
    """A question is answered by the id the interface printed, or not at all."""

    runtime = asking_runtime(tmp_path, "http.db")
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        cockpit=CockpitService(runtime, phase6_enabled=True, master_id="operator"),
    ).start()
    try:
        snapshot = CockpitService(
            runtime, phase6_enabled=True, master_id="operator"
        ).snapshot()
        question_id = snapshot["being"]["self"]["unresolved_questions"][0]["id"]
        path = f"/cockpit/api/questions/{question_id}/answer"

        status, _ = await post(server.bound_port, path, {"answer": "Use target A"}, token=None)
        assert status == 401

        status, _ = await post(server.bound_port, path, {"answer": "   "})
        assert status == 400

        status, payload = await post(server.bound_port, path, {"answer": "Use target A"})
        assert status == 200, payload
        assert payload["event_type"] == "self_question_answered"
        await runtime.run_pending()
        assert runtime.state_store.get("self", "unresolved_questions") == []

        status, _ = await post(server.bound_port, path, {"answer": "Use target A"})
        assert status == 404
    finally:
        await server.stop()
        runtime.close()


async def post(port, path, body, *, token=TOKEN):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"POST {path} HTTP/1.1", "Host: 127.0.0.1"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    lines.append("Content-Type: application/json")
    lines.append(f"Content-Length: {len(payload)}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, raw = response.partition(b"\r\n\r\n")
    return int(head.split(b"\r\n", 1)[0].split()[1]), json.loads(raw)
