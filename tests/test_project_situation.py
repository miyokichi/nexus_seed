"""Project Situation is a restart-safe, read-only projection over existing state."""

from __future__ import annotations

import asyncio
import json

from nexus_seed.adapters.webhook import WebhookIngress, WebhookServer
from nexus_seed.cockpit import CockpitService
from nexus_seed.control.models import Goal
from nexus_seed.core.continuation import Continuation
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessInstance, ProcessStatus
from nexus_seed.presence.models import IntentionRecord
from nexus_seed.projects import (
    ProjectOverallStatus,
    get_project_situation,
    get_project_summaries,
)
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus
from nexus_seed.world.state_delta import StateDelta


TOKEN = "project-test-token"


async def _get(port: int, path: str, *, token: str | None = None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    headers = [f"GET {path} HTTP/1.1", "Host: 127.0.0.1"]
    if token:
        headers.append(f"Authorization: Bearer {token}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, body = response.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(body)


def _goal(project_id: str, *, title: str = "Review readiness") -> Goal:
    return Goal(
        title=title,
        objective="Make the project ready for review",
        owner_identity_id="operator",
        metadata={
            "project_id": project_id,
            "project_title": "Project A" if project_id == "project-a" else title,
            "project_objective": "Reach review readiness",
        },
    )


def _save_intention(runtime: Runtime, goal: Goal, focus: str) -> IntentionRecord:
    intention = IntentionRecord.for_goal(goal.id, focus)
    runtime.state_store.set(f"intention:{intention.id}", "record", intention.to_dict())
    return intention


def test_project_situation_aggregates_only_explicit_relationships_and_restarts(tmp_path):
    database = tmp_path / "projects.db"
    runtime = Runtime(database)
    goal = _goal("project-a")
    unrelated_goal = _goal("project-b", title="Unrelated project")
    runtime.control_store.save_goal(goal)
    runtime.control_store.save_goal(unrelated_goal)
    intention = _save_intention(runtime, goal, "Resolve measurement uncertainty")
    _save_intention(runtime, unrelated_goal, "Do unrelated work")

    source = runtime.event_store.append(
        Event(
            "deadline_updated",
            "test",
            {"project_id": "project-a", "goal_id": str(goal.id)},
        )
    )
    runtime.event_store.append(
        Event("unrelated_change", "test", {"project_id": "project-b"})
    )
    active = WorkRequirement(
        work_type="analyze_measurement",
        work_key="project-a:active",
        objective="Analyze latest measurement",
        goal_id=goal.id,
        source_event_id=source.id,
    )
    blocked = WorkRequirement(
        work_type="wait_for_team",
        work_key="project-a:blocked",
        objective="Wait for Team B response",
        goal_id=goal.id,
        status=WorkStatus.BLOCKED_PROVIDER,
    )
    completed = WorkRequirement(
        work_type="capture_assumptions",
        work_key="project-a:completed",
        objective="Capture assumptions",
        goal_id=goal.id,
        status=WorkStatus.SATISFIED,
    )
    unrelated = WorkRequirement(
        work_type="unrelated",
        work_key="project-b:work",
        objective="Must not leak",
        goal_id=unrelated_goal.id,
    )
    for work in (active, blocked, completed, unrelated):
        runtime.work_requirement_store.save(work)
    delta = runtime.state_delta_store.save(
        StateDelta(
            entity="project:project-a", attribute="deadline", old_value="later",
            new_value="earlier", source_event_id=source.id,
            reason="deadline moved earlier",
        )
    )
    runtime.state_store.set(
        "project:project-a",
        "deadline",
        "earlier",
        source_event=source.id,
        state_delta_id=delta.id,
    )
    runtime.state_store.set(
        "self",
        "unresolved_questions",
        [
            {"project_id": "project-a", "question": "Which assumption is authoritative?"},
            {"project_id": "project-b", "question": "Unrelated question"},
        ],
    )

    before = runtime.db.conn.total_changes
    situation = get_project_situation(runtime, "project-a")
    assert runtime.db.conn.total_changes == before
    assert situation is not None
    assert runtime.services.get_project_situation("project-a").to_dict() == situation.to_dict()
    assert situation.overall_status is ProjectOverallStatus.BLOCKED
    assert situation.title == "Project A"
    assert [item["id"] for item in situation.active_goals] == [str(goal.id)]
    assert [item["id"] for item in situation.current_intentions] == [str(intention.id)]
    assert [item["id"] for item in situation.active_work] == [str(active.id)]
    assert [item["id"] for item in situation.blocked_work] == [str(blocked.id)]
    assert [item["id"] for item in situation.recently_completed_work] == [str(completed.id)]
    assert any(item["type"] == "BLOCKED_PROVIDER" for item in situation.blockers)
    assert [item["text"] for item in situation.unresolved_questions] == [
        "Which assumption is authoritative?"
    ]
    assert {item["type"] for item in situation.recent_events} == {"deadline_updated"}
    assert any(item["type"] == "WORLD_STATE" for item in situation.recent_changes)
    assert "1 work item is blocked" in situation.summary
    first = situation.to_dict()
    runtime.close()

    restarted = Runtime(database)
    try:
        assert get_project_situation(restarted, "project-a").to_dict() == first
    finally:
        restarted.close()


def test_pending_review_means_needs_attention_without_hard_blocker(tmp_path):
    runtime = Runtime(tmp_path / "review.db")
    try:
        goal = _goal("project-review", title="Review project")
        runtime.control_store.save_goal(goal)
        work = WorkRequirement(
            work_type="reviewable_work",
            work_key="project-review:work",
            objective="Confirm proposed change",
            goal_id=goal.id,
            status=WorkStatus.WAITING_REVIEW,
        )
        runtime.work_requirement_store.save(work)
        process = ProcessInstance(
            "review_human_work",
            "1",
            status=ProcessStatus.SUSPENDED,
            work_requirement_id=work.id,
        )
        runtime.process_store.save_instance(process)
        runtime.continuation_store.save(
            Continuation(
                process_instance_id=process.id,
                resume_point="await_work_review",
                waiting_for={
                    "event_type": "work_reviewed",
                    "work_requirement_id": str(work.id),
                },
            )
        )

        situation = get_project_situation(runtime, "project-review")
        assert situation.overall_status is ProjectOverallStatus.NEEDS_ATTENTION
        assert len(situation.pending_reviews) == 1
        assert any(item["type"] == "PENDING_REVIEW" for item in situation.blockers)
    finally:
        runtime.close()


def test_unresolved_dependency_and_failed_process_are_project_blockers(tmp_path):
    runtime = Runtime(tmp_path / "failed.db")
    try:
        goal = _goal("project-failed", title="Failed project")
        runtime.control_store.save_goal(goal)
        work = WorkRequirement(
            work_type="dependent_work",
            work_key="project-failed:work",
            objective="Use missing input",
            goal_id=goal.id,
            metadata={"depends_on": ["missing-work-key"]},
        )
        runtime.work_requirement_store.save(work)
        runtime.process_store.save_instance(
            ProcessInstance(
                "project_worker",
                "1",
                status=ProcessStatus.FAILED,
                work_requirement_id=work.id,
                last_error="provider failed",
            )
        )

        situation = get_project_situation(runtime, "project-failed")
        assert situation.overall_status is ProjectOverallStatus.BLOCKED
        assert {item["type"] for item in situation.blockers} >= {
            "UNRESOLVED_DEPENDENCY",
            "FAILED_WORK",
        }
    finally:
        runtime.close()


def test_legacy_records_without_project_metadata_remain_unassigned(tmp_path):
    runtime = Runtime(tmp_path / "legacy.db")
    try:
        runtime.control_store.save_goal(
            Goal(
                title="Legacy goal",
                objective="Continue working",
                owner_identity_id="operator",
            )
        )
        runtime.work_requirement_store.save(
            WorkRequirement(work_type="legacy", work_key="legacy:work")
        )
        runtime.event_store.append(Event("legacy_event", "test", {"subject": "unknown"}))

        before = runtime.db.conn.total_changes
        assert get_project_summaries(runtime) == []
        assert get_project_situation(runtime, "guessed-project") is None
        assert runtime.db.conn.total_changes == before
        assert len(runtime.control_store.goals()) == 1
        assert len(runtime.work_requirement_store.all()) == 1
    finally:
        runtime.close()


async def test_project_http_api_is_authenticated_and_read_only(tmp_path):
    runtime = Runtime(tmp_path / "api.db")
    goal = _goal("project-a")
    runtime.control_store.save_goal(goal)
    cockpit = CockpitService(runtime, phase6_enabled=False, master_id="operator")
    server = await WebhookServer(
        WebhookIngress(runtime.ingress, token=TOKEN), cockpit=cockpit
    ).start()
    try:
        status, body = await _get(server.bound_port, "/projects")
        assert status == 401 and body["error"] == "unauthorized"

        before = runtime.db.conn.total_changes
        status, body = await _get(server.bound_port, "/projects", token=TOKEN)
        assert status == 200
        assert body["projects"][0]["project_id"] == "project-a"
        # A Goal that has not generated Work yet is still being planned.
        assert body["projects"][0]["status"] == "PLANNING"

        status, body = await _get(
            server.bound_port, "/projects/project-a/situation", token=TOKEN
        )
        assert status == 200
        assert body["project_id"] == "project-a"
        assert body["active_goals"][0]["id"] == str(goal.id)
        assert runtime.db.conn.total_changes == before

        status, body = await _get(
            server.bound_port, "/projects/unknown/situation", token=TOKEN
        )
        assert status == 404 and body["error"] == "project not found"
    finally:
        await server.stop()
        runtime.close()
