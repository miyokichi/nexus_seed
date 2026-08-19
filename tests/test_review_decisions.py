"""Deciding a review needs no command surface behind it.

A review is a Continuation waiting for an event whose type ends in
``_reviewed``.  These tests pin that a person can settle one through the plain
review path — and keep being able to once the Control Plane is gone.
"""

from __future__ import annotations

import pytest

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.reviews import (
    ReviewDecisionError,
    decide_review,
    find_review,
    pending_reviews,
)
from nexus_seed.runtime.runtime import Runtime


pytestmark = pytest.mark.asyncio


NEEDS_REVIEW = ProcessDefinition(
    name="needs_review",
    version="1",
    handler="needs_review",
    trigger_event_types=("thing_happened",),
)


async def needs_review(ctx):
    if ctx.resume_point is None:
        return ctx.suspend(
            resume_point="awaiting_decision",
            waiting_for={
                "event_type": "thing_reviewed",
                "thing_id": ctx.event.payload["thing_id"],
            },
        )
    return ctx.complete(
        emitted_events=[
            ctx.new_event("thing_settled", {"decision": ctx.event.payload.get("decision")})
        ]
    )


def review_runtime(tmp_path, name: str) -> Runtime:
    runtime = Runtime(tmp_path / name)
    runtime.register_process(NEEDS_REVIEW, needs_review)
    return runtime


def settled(runtime) -> list[str]:
    """The decisions the resumed process actually saw."""

    return [
        event.payload["decision"]
        for event in runtime.event_store.all()
        if event.type == "thing_settled"
    ]


async def waiting(runtime, thing_id: str = "thing-1"):
    await runtime.submit_event(Event("thing_happened", "test", {"thing_id": thing_id}))
    return pending_reviews(runtime)


async def test_a_waiting_continuation_is_what_a_review_is(tmp_path):
    runtime = review_runtime(tmp_path, "waiting.db")
    try:
        reviews = await waiting(runtime)
        assert len(reviews) == 1
        review = reviews[0]
        assert review.review_id == "thing-1"
        assert review.event_type == "thing_reviewed"
        assert review.condition == {"thing_id": "thing-1"}
    finally:
        runtime.close()


async def test_approving_resumes_the_process(tmp_path):
    runtime = review_runtime(tmp_path, "approve.db")
    try:
        await waiting(runtime)
        event = await decide_review(runtime, "thing-1", "approve", actor="operator")
        assert event is not None
        assert event.type == "thing_reviewed"
        assert event.payload == {"thing_id": "thing-1", "decision": "approve"}
        assert event.source == "human:operator"
        instances = runtime.process_store.all_instances()
        assert [item.status for item in instances] == [ProcessStatus.COMPLETED]
        assert settled(runtime) == ["approve"]
        assert pending_reviews(runtime) == []
    finally:
        runtime.close()


async def test_rejecting_resumes_the_process_with_the_other_answer(tmp_path):
    runtime = review_runtime(tmp_path, "reject.db")
    try:
        await waiting(runtime)
        event = await decide_review(runtime, "thing-1", "reject")
        assert event is not None
        assert event.payload["decision"] == "reject"
        assert settled(runtime) == ["reject"]
    finally:
        runtime.close()


async def test_a_note_travels_with_the_decision(tmp_path):
    runtime = review_runtime(tmp_path, "note.db")
    try:
        await waiting(runtime)
        event = await decide_review(runtime, "thing-1", "approve", note="looks right")
        assert event is not None
        assert event.payload["note"] == "looks right"
    finally:
        runtime.close()


async def test_deciding_twice_changes_nothing_the_second_time(tmp_path):
    """A double-clicked approve button must not emit a second event."""

    runtime = review_runtime(tmp_path, "twice.db")
    try:
        await waiting(runtime)
        assert await decide_review(runtime, "thing-1", "approve") is not None
        events_after_first = len(runtime.event_store.all())
        assert await decide_review(runtime, "thing-1", "approve") is None
        assert len(runtime.event_store.all()) == events_after_first
    finally:
        runtime.close()


async def test_an_unknown_review_is_not_an_error(tmp_path):
    runtime = review_runtime(tmp_path, "unknown.db")
    try:
        assert await decide_review(runtime, "no-such-thing", "approve") is None
        assert find_review(runtime, "no-such-thing") is None
    finally:
        runtime.close()


async def test_an_ambiguous_reference_refuses_rather_than_guesses(tmp_path):
    runtime = review_runtime(tmp_path, "ambiguous.db")
    try:
        await waiting(runtime, "same-id")
        await waiting(runtime, "same-id")
        assert len(pending_reviews(runtime)) == 2
        with pytest.raises(ReviewDecisionError):
            await decide_review(runtime, "same-id", "approve")
    finally:
        runtime.close()


async def test_only_approve_and_reject_are_decisions(tmp_path):
    runtime = review_runtime(tmp_path, "vocabulary.db")
    try:
        await waiting(runtime)
        with pytest.raises(ReviewDecisionError):
            await decide_review(runtime, "thing-1", "maybe")
        assert len(pending_reviews(runtime)) == 1
    finally:
        runtime.close()


async def test_reviews_are_decidable_with_the_control_plane_off(tmp_path):
    runtime = Runtime(tmp_path / "control-off.db", control_enabled=False)
    runtime.register_process(NEEDS_REVIEW, needs_review)
    try:
        assert runtime.console is None
        await waiting(runtime)
        assert await decide_review(runtime, "thing-1", "approve") is not None
        assert runtime.process_store.all_instances()[0].status is ProcessStatus.COMPLETED
    finally:
        runtime.close()


# --- HTTP -----------------------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer  # noqa: E402
from nexus_seed.cockpit.service import CockpitService  # noqa: E402

TOKEN = "review-token"


async def _request(port, method, path, *, token=TOKEN, body=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else b""
    if payload:
        lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(payload)}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, raw = response.partition(b"\r\n\r\n")
    return int(head.split(b"\r\n", 1)[0].split()[1]), json.loads(raw)


async def test_http_review_decision(tmp_path):
    runtime = review_runtime(tmp_path, "http.db")
    server = WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN),
        host="127.0.0.1",
        port=0,
        cockpit=CockpitService(runtime, phase6_enabled=False, master_id="local-operator"),
    )
    await server.start()
    port = server.bound_port
    try:
        await waiting(runtime)
        base = "/cockpit/api/reviews/thing-1"

        status, _ = await _request(port, "POST", f"{base}/approve", token=None)
        assert status == 401

        status, _ = await _request(port, "POST", f"{base}/maybe")
        assert status == 404

        status, payload = await _request(port, "POST", f"{base}/approve",
                                         body={"note": "確認しました"})
        assert status == 200, payload
        assert payload["decision"] == "approve"
        assert payload["event_type"] == "thing_reviewed"
        assert settled(runtime) == ["approve"]

        # The same call again finds nothing waiting and says so.
        status, payload = await _request(port, "POST", f"{base}/approve")
        assert status == 404
        assert settled(runtime) == ["approve"]
    finally:
        await server.stop()
        runtime.close()
