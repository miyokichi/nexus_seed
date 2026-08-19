"""A Project is one Goal plus its Work, created with the Goal and derived after."""

from __future__ import annotations

import json
import uuid

from nexus_seed.backends.llm import FakeLLMBackend, proposal_response
from nexus_seed.chat.service import ProjectChatService
from nexus_seed.cockpit import CockpitService
from nexus_seed.control.models import Goal, GoalStatus
from nexus_seed.goals import create_goal, end_goal
from nexus_seed.core.continuation import Continuation
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessInstance, ProcessStatus
from nexus_seed.processes.control import bootstrap_control
from nexus_seed.projects import (
    ProjectOverallStatus,
    get_project_situation,
    get_project_summaries,
    project_id_for_goal,
    project_id_of,
)
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


CRITERIA = json.dumps(
    [
        {
            "type": "required_work",
            "semantic_key": "analyze",
            "objective": "測定結果を解析する",
            "work_type": "analysis_work",
            "required_capabilities": ["advance_human_goal"],
        },
        {
            "type": "required_work",
            "semantic_key": "report",
            "objective": "レポートを作成する",
            "work_type": "report_work",
            "required_capabilities": ["advance_human_goal"],
        },
    ],
    separators=(",", ":"),
)


def _runtime(tmp_path, name="lifecycle.db") -> Runtime:
    runtime = Runtime(tmp_path / name)
    bootstrap_control(runtime)
    return runtime


async def _create_goal(runtime, *, objective="Project Aをレビュー可能にする", criteria=CRITERIA,
                       title="Project A readiness", message_id="goal-1"):
    goal = await create_goal(
        runtime,
        objective,
        title=title,
        owner="operator",
        success_criteria=json.loads(criteria) if criteria else (),
    )
    await runtime.run_pending()
    return goal


async def _end_goal(runtime, goal_id, status):
    result = await end_goal(runtime, goal_id, status, owner="operator")
    await runtime.run_pending()
    return result


async def test_creating_a_goal_creates_exactly_one_project(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        created = await _create_goal(runtime)
        goal_id = created.id
        goal = runtime.control_store.get_goal(goal_id)

        expected = project_id_for_goal(goal_id)
        assert project_id_of(created) == expected
        assert project_id_of(goal) == expected

        summaries = get_project_summaries(runtime)
        assert [item.project_id for item in summaries] == [expected]
        assert summaries[0].root_goal_id == str(goal_id)
        assert summaries[0].title == "Project A readiness"
        assert summaries[0].objective == "Project Aをレビュー可能にする"

        situation = get_project_situation(runtime, expected)
        assert situation.project.root_goal_id == str(goal_id)
        assert situation.goal["id"] == str(goal_id)
        assert situation.project.created_at == goal.created_at

        created_event = runtime.event_store.by_type("goal_created")[-1]
        assert created_event.payload["project_id"] == expected
    finally:
        runtime.close()


async def test_two_goals_are_two_projects_and_never_share_work(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        first = await _create_goal(runtime, title="First goal", message_id="goal-1")
        second = await _create_goal(
            runtime,
            title="Second goal",
            objective="別の目的を達成する",
            message_id="goal-2",
        )
        first_id = project_id_for_goal(first.id)
        second_id = project_id_for_goal(second.id)

        assert sorted(item.project_id for item in get_project_summaries(runtime)) == sorted(
            [first_id, second_id]
        )
        first_situation = get_project_situation(runtime, first_id)
        second_situation = get_project_situation(runtime, second_id)
        first_work = {item["id"] for item in first_situation.remaining_tasks}
        second_work = {item["id"] for item in second_situation.remaining_tasks}
        assert first_work and second_work
        assert first_work.isdisjoint(second_work)
        assert first_situation.goal["id"] == str(first.id)
        assert second_situation.goal["id"] == str(second.id)
    finally:
        runtime.close()


async def test_goal_work_joins_the_project_and_survives_restart_without_duplication(tmp_path):
    database = tmp_path / "restart.db"
    runtime = _runtime(tmp_path, name="restart.db")
    try:
        created = await _create_goal(runtime)
        goal_id = created.id
        project_id = project_id_for_goal(goal_id)
        works = runtime.work_requirement_store.for_goal(goal_id)
        assert len(works) == 2
        assert {work.project for work in works} == {project_id}
    finally:
        runtime.close()

    restarted = _runtime_reopen(database)
    try:
        # Re-evaluating after restart converges on the same Project and Work.
        await restarted.submit_event(
            Event("goal_evaluation_requested", "test", {"goal_id": str(goal_id)})
        )
        assert [item.project_id for item in get_project_summaries(restarted)] == [project_id]
        works = restarted.work_requirement_store.for_goal(goal_id)
        assert len(works) == 2
        assert {work.project for work in works} == {project_id}

        situation = get_project_situation(restarted, project_id)
        assert len(situation.remaining_tasks) == 2
        assert situation.project.root_goal_id == str(goal_id)
    finally:
        restarted.close()


def _runtime_reopen(database) -> Runtime:
    runtime = Runtime(database)
    bootstrap_control(runtime)
    return runtime


async def test_replanned_and_later_work_stay_in_the_same_project(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        created = await _create_goal(runtime)
        goal_id = created.id
        project_id = project_id_for_goal(goal_id)
        work = runtime.work_requirement_store.for_goal(goal_id)[0]

        # Replanning re-plans the same durable need; it never re-homes it.
        runtime.work_requirement_store.record_replan(work.id, 1)
        replanned = runtime.work_requirement_store.get(work.id)
        assert replanned.replan_count == 1
        assert replanned.project == project_id

        situation = get_project_situation(runtime, project_id)
        assert str(work.id) in {item["id"] for item in situation.remaining_tasks}

        # Work discovered later for the same Goal joins the same Project.
        later = WorkRequirement(
            work_type="follow_up",
            work_key=f"goal:{goal_id}:follow-up",
            objective="追加で判明した作業",
            goal_id=goal_id,
        )
        runtime.work_requirement_store.save(later)
        situation = get_project_situation(runtime, project_id)
        assert str(later.id) in {item["id"] for item in situation.remaining_tasks}
    finally:
        runtime.close()


async def test_one_single_task_is_already_a_project(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        created = await _create_goal(runtime, criteria=None, title="Single task goal")
        goal_id = created.id
        project_id = project_id_for_goal(goal_id)

        works = runtime.work_requirement_store.for_goal(goal_id)
        assert len(works) == 1

        situation = get_project_situation(runtime, project_id)
        assert situation.overall_status is ProjectOverallStatus.ACTIVE
        assert len(situation.remaining_tasks) == 1
        assert situation.goal["id"] == str(goal_id)
    finally:
        runtime.close()


async def test_project_status_follows_work_reviews_and_goal_lifecycle(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        created = await _create_goal(runtime)
        goal_id = created.id
        project_id = project_id_for_goal(goal_id)
        works = runtime.work_requirement_store.for_goal(goal_id)

        assert get_project_situation(runtime, project_id).overall_status is (
            ProjectOverallStatus.ACTIVE
        )

        runtime.work_requirement_store.update_status(
            works[0].id, WorkStatus.BLOCKED_CAPABILITY
        )
        situation = get_project_situation(runtime, project_id)
        assert situation.overall_status is ProjectOverallStatus.BLOCKED
        assert [item["id"] for item in situation.blocked_tasks] == [str(works[0].id)]

        runtime.work_requirement_store.update_status(works[0].id, WorkStatus.SATISFIED)
        process = ProcessInstance(
            "review_human_work",
            "1",
            status=ProcessStatus.SUSPENDED,
            work_requirement_id=works[1].id,
        )
        runtime.process_store.save_instance(process)
        runtime.continuation_store.save(
            Continuation(
                process_instance_id=process.id,
                resume_point="await_work_review",
                waiting_for={
                    "event_type": "work_reviewed",
                    "work_requirement_id": str(works[1].id),
                },
            )
        )
        situation = get_project_situation(runtime, project_id)
        assert situation.overall_status is ProjectOverallStatus.NEEDS_ATTENTION
        assert situation.completed_total == 1
    finally:
        runtime.close()


async def test_goal_pause_resume_and_cancel_reach_the_project(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        created = await _create_goal(runtime)
        goal_id = created.id
        project_id = project_id_for_goal(goal_id)

        await _end_goal(runtime, goal_id, GoalStatus.PAUSED)
        assert get_project_situation(runtime, project_id).overall_status is (
            ProjectOverallStatus.PAUSED
        )

        await _end_goal(runtime, goal_id, GoalStatus.ACTIVE)
        assert get_project_situation(runtime, project_id).overall_status is (
            ProjectOverallStatus.ACTIVE
        )

        await _end_goal(runtime, goal_id, GoalStatus.CANCELLED)
        situation = get_project_situation(runtime, project_id)
        assert situation.overall_status is ProjectOverallStatus.CANCELLED
        assert situation.project.status is ProjectOverallStatus.CANCELLED
        # The Goal remains the only lifecycle; the project never diverges.
        assert runtime.control_store.get_goal(goal_id).status is GoalStatus.CANCELLED
    finally:
        runtime.close()


async def test_planning_becomes_completed_when_the_goal_is_achieved(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        goal = Goal(
            title="Deferred goal",
            objective="あとで着手する",
            owner_identity_id="operator",
            metadata={"project_id": "project-deferred"},
        )
        runtime.control_store.save_goal(goal)
        assert get_project_situation(runtime, "project-deferred").overall_status is (
            ProjectOverallStatus.PLANNING
        )

        work = WorkRequirement(
            work_type="single",
            work_key="project-deferred:work",
            objective="唯一の作業",
            goal_id=goal.id,
            status=WorkStatus.SATISFIED,
        )
        runtime.work_requirement_store.save(work)
        runtime.control_store.update_goal_status(goal.id, GoalStatus.ACHIEVED)

        situation = get_project_situation(runtime, "project-deferred")
        assert situation.overall_status is ProjectOverallStatus.COMPLETED
        assert situation.remaining_tasks == ()
        assert [item["id"] for item in situation.completed_tasks] == [str(work.id)]
    finally:
        runtime.close()


async def test_situation_and_chat_work_on_an_auto_created_project(tmp_path):
    runtime = _runtime(tmp_path)
    backend = FakeLLMBackend(
        [
            proposal_response(
                {
                    "answer": "解析とレポートの2件が残っています。",
                    "certainty": "FACT",
                    "references": [],
                }
            )
        ]
    )
    runtime.register_backend("llm", backend)
    try:
        created = await _create_goal(runtime)
        goal_id = created.id
        project_id = project_id_for_goal(goal_id)

        cockpit = CockpitService(runtime, phase6_enabled=False, master_id="operator")
        listed = cockpit.projects()
        assert [item["project_id"] for item in listed] == [project_id]
        assert listed[0]["remaining_tasks"] == 2

        payload = cockpit.project_situation(project_id)
        assert payload["project"]["root_goal_id"] == str(goal_id)
        assert payload["goal"]["title"] == "Project A readiness"
        assert len(payload["remaining_tasks"]) == 2

        reply = await ProjectChatService(runtime).ask(project_id, "残りタスクは？")
        assert reply["status"] == "ANSWERED"
        context = backend.calls[-1].context["project_situation"]
        assert context["root_goal"]["id"] == str(goal_id)
        assert len(context["active_work"]) == 2
    finally:
        runtime.close()


def test_goals_created_outside_the_control_plane_stay_unassigned(tmp_path):
    runtime = Runtime(tmp_path / "legacy.db")
    try:
        runtime.control_store.save_goal(
            Goal(title="Legacy", objective="続行", owner_identity_id="operator")
        )
        assert get_project_summaries(runtime) == []
    finally:
        runtime.close()
