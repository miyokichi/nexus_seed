"""The whole loop, end to end: Goal -> Project -> World -> Work -> Capability
-> Execution -> Evaluation -> Goal.

Each case drives the *existing* subsystems through one complete turn of the
cycle and checks the turn from the outside: the Goal, the Project, the World
State, and — when the loop cannot turn on its own — what the human is told.
"""

from __future__ import annotations

import json
import uuid

from capability_helpers import work_pipeline
from extension_helpers import POWERPOINT, extension_runtime, needs, register_disabled_provider

from nexus_seed.capabilities.models import CapabilityRef, CapabilityRequirement
from nexus_seed.control.models import GoalStatus, HumanIdentity
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.orchestration import (
    HUMAN_INTERVENTION_REQUIRED,
    GoalLoopStage,
    bootstrap_orchestration,
    get_goal_loop,
)
from nexus_seed.processes.autonomy import bootstrap_autonomy
from nexus_seed.processes.control import bootstrap_control
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import bootstrap_work_intelligence
from nexus_seed.projects import (
    ProjectOverallStatus,
    get_project_situation,
    project_id_for_goal,
)
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus


def review_ready_criteria(*, entity: str = "project-a", capability: str = "prepare_review") -> str:
    """A Goal that is satisfied by the world, and names the Work that gets it there."""

    return json.dumps(
        [
            {
                "type": "state_predicate",
                "entity": entity,
                "attribute": "review_ready",
                "operator": "==",
                "value": True,
                "work": {
                    "semantic_key": "prepare-review",
                    "objective": "レビュー準備を完了する",
                    "work_type": "prepare_review",
                    "required_capabilities": [capability],
                },
            }
        ],
        separators=(",", ":"),
    )


def authorize(runtime) -> None:
    runtime.control_store.save_identity(
        HumanIdentity("operator", "operator", ("command.*",))
    )


def goal_runtime(tmp_path, name: str = "loop.db") -> Runtime:
    """The ordinary application stack: world model, work planning, goals, loop."""

    runtime = Runtime(tmp_path / name)
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    bootstrap_control(runtime)
    bootstrap_orchestration(runtime)
    authorize(runtime)
    return runtime


async def create_goal(runtime, *, criteria: str, title: str = "Review readiness",
                      objective: str = "レビュー可能な状態にする", message_id: str = "goal-1"):
    created = runtime.console.execute_text(
        f'/goal create title="{title}" objective="{objective}" success=\'{criteria}\'',
        issuer_identity_id="operator",
        source_message_id=message_id,
    )
    await runtime.run_pending()
    return uuid.UUID(created.data["id"])


def register_worker(runtime, capability: str, handler, *, name: str | None = None) -> None:
    runtime.register_process(
        ProcessDefinition(
            name or capability,
            "1",
            name or capability,
            provides_capabilities=(CapabilityRef(capability),),
        ),
        handler,
    )


def _finish_by_changing_the_world(ctx, entity: str, attribute: str, value):
    """Report a result the ordinary way: propose a delta, then satisfy the Work."""

    delta = ctx.propose_delta(
        entity=entity,
        attribute=attribute,
        old_value=ctx.state.get(entity, attribute),
        new_value=value,
        reason="task completed",
    )
    ctx.satisfy_work()
    return ctx.complete(
        output={"entity": entity, "attribute": attribute},
        emitted_events=[
            ctx.new_event(
                "state_delta_created",
                {
                    "entity": delta.entity,
                    "attribute": delta.attribute,
                    "old_value": delta.old_value,
                    "new_value": delta.new_value,
                    "state_delta_id": str(delta.id),
                    "confidence": 1.0,
                },
            ),
            ctx.new_event(
                "work_satisfied",
                {"work_requirement_id": str(ctx.instance.work_requirement_id)},
            ),
        ],
    )


def world_updating_worker(entity: str, attribute: str, value):
    """A Task that does its job by changing the world, the ordinary way."""

    async def handler(ctx):
        return _finish_by_changing_the_world(ctx, entity, attribute, value)

    return handler


def measurement_waiting_worker(entity: str, attribute: str, value):
    """A Task that cannot finish until the world tells it something."""

    async def handler(ctx):
        if ctx.resume_point is None and ctx.state.get(entity, "measurement") is None:
            return ctx.suspend(
                resume_point="after_measurement",
                waiting_for={"event_type": "measurement_completed", "project": entity},
                saved_process_state={"entity": entity},
            )
        return _finish_by_changing_the_world(ctx, entity, attribute, value)

    return handler


# --- Case A: the ordinary turn of the loop ---------------------------------


async def test_case_a_goal_becomes_project_work_execution_and_completion(tmp_path):
    runtime = goal_runtime(tmp_path)
    register_worker(
        runtime, "prepare_review", world_updating_worker("project-a", "review_ready", True)
    )
    try:
        goal_id = await create_goal(runtime, criteria=review_ready_criteria())
        project_id = project_id_for_goal(goal_id)

        # Goal -> Project -> Work: the Project exists and owns the Work.
        works = runtime.work_requirement_store.for_goal(goal_id)
        assert [work.project for work in works] == [project_id]
        assert [work.status for work in works] == [WorkStatus.SATISFIED]

        # Execution: the Task ran through the ordinary Provider boundary.
        selected = runtime.db.query(
            "SELECT process_definition_name FROM provider_selections "
            "WHERE process_definition_name = 'prepare_review'"
        )
        assert selected

        # Execution -> World: the Task changed the world through the normal
        # StateDelta pipeline, not by writing state itself.
        assert runtime.state_store.get("project-a", "review_ready") is True

        # World -> Goal -> Project: re-evaluation closed the Goal and the Project.
        assert runtime.control_store.get_goal(goal_id).status is GoalStatus.ACHIEVED
        situation = get_project_situation(runtime, project_id)
        assert situation.overall_status is ProjectOverallStatus.COMPLETED
        assert situation.completed_total == 1

        loop = get_goal_loop(runtime, goal_id)
        assert loop.stage is GoalLoopStage.ACHIEVED
        assert loop.needs_human is False
        assert loop.completed_tasks == 1

        types = [event.type for event in runtime.event_store.all()]
        for expected in (
            "goal_created",
            "goal_work_generated",
            "work_required",
            "work_matched",
            "work_spawned",
            "state_changed",
            "work_satisfied",
            "goal_achieved",
        ):
            assert expected in types
    finally:
        runtime.close()


# --- Case B: the capability the loop did not have --------------------------


async def test_case_b_missing_capability_is_acquired_and_the_goal_completes(tmp_path):
    runtime = extension_runtime(tmp_path, "case-b.db")
    bootstrap_control(runtime)
    bootstrap_autonomy(runtime)
    bootstrap_orchestration(runtime)
    authorize(runtime)
    register_disabled_provider(runtime, "review_preparer", "prepare_review")
    try:
        goal_id = await create_goal(runtime, criteria=review_ready_criteria())
        project_id = project_id_for_goal(goal_id)
        work = runtime.work_requirement_store.for_goal(goal_id)[0]

        # The Capability subsystem was reached, acquired, and handed the Work back.
        session = runtime.autonomy_store.sessions_for_work(work.id)[0]
        assert session.status.value == "COMPLETED"
        assert runtime.get_work_requirement(work.id).status is WorkStatus.SATISFIED
        assert runtime.get_capability_gaps_for_work(work.id)

        loop = get_goal_loop(runtime, goal_id)
        assert loop.stage is not GoalLoopStage.HUMAN_REQUIRED
        assert get_project_situation(runtime, project_id).blocked_tasks == ()
        assert not runtime.event_store.by_type(HUMAN_INTERVENTION_REQUIRED)
    finally:
        runtime.close()


# --- Case C: the world moved, so the plan moved ----------------------------


async def test_case_c_external_event_changes_the_world_and_replans(tmp_path):
    runtime = goal_runtime(tmp_path, "case-c.db")
    register_worker(
        runtime,
        "prepare_review",
        measurement_waiting_worker("project-a", "review_ready", True),
    )
    register_worker(
        runtime, "clear_risk", world_updating_worker("project-a", "risk_cleared", True)
    )
    criteria = json.dumps(
        [
            {
                "type": "state_predicate",
                "entity": "project-a",
                "attribute": "review_ready",
                "operator": "==",
                "value": True,
                "work": {
                    "semantic_key": "prepare-review",
                    "objective": "レビュー準備を完了する",
                    "work_type": "prepare_review",
                    "required_capabilities": ["prepare_review"],
                },
            },
            {
                "type": "state_predicate",
                "entity": "project-a",
                "attribute": "risk_cleared",
                "operator": "==",
                "value": True,
                "work": {
                    "semantic_key": "clear-risk",
                    "objective": "リスクを解消する",
                    "work_type": "clear_risk",
                    "required_capabilities": ["clear_risk"],
                },
            },
        ],
        separators=(",", ":"),
    )
    try:
        # The world already answers the second criterion, so only one Task exists.
        runtime.state_store.set("project-a", "risk_cleared", True)
        goal_id = await create_goal(runtime, criteria=criteria)
        works = runtime.work_requirement_store.for_goal(goal_id)
        assert [work.work_type for work in works] == ["prepare_review"]
        assert works[0].status is WorkStatus.SPAWNED
        assert runtime.control_store.get_goal(goal_id).status is GoalStatus.ACTIVE
        assert get_goal_loop(runtime, goal_id).stage is GoalLoopStage.EXECUTING

        # An external event moves the world, so the gap — and the plan — change.
        await runtime.submit_event(
            Event(
                "state_delta_created",
                "external",
                {
                    "entity": "project-a",
                    "attribute": "risk_cleared",
                    "old_value": True,
                    "new_value": False,
                    "confidence": 1.0,
                },
            )
        )
        replanned = runtime.work_requirement_store.for_goal(goal_id)
        assert sorted(work.work_type for work in replanned) == [
            "clear_risk",
            "prepare_review",
        ]
        risk_work = next(item for item in replanned if item.work_type == "clear_risk")
        assert risk_work.status is WorkStatus.SATISFIED
        assert risk_work.project == project_id_for_goal(goal_id)
        assert runtime.state_store.get("project-a", "risk_cleared") is True

        # The waiting Task resumes on the measurement it was suspended for.
        await runtime.submit_event(
            Event("measurement_completed", "external", {"project": "project-a"})
        )

        assert runtime.state_store.get("project-a", "review_ready") is True
        assert all(
            work.status is WorkStatus.SATISFIED
            for work in runtime.work_requirement_store.for_goal(goal_id)
        )
        assert runtime.control_store.get_goal(goal_id).status is GoalStatus.ACHIEVED
        situation = get_project_situation(runtime, project_id_for_goal(goal_id))
        assert situation.overall_status is ProjectOverallStatus.COMPLETED
        assert situation.completed_total == 2
    finally:
        runtime.close()


# --- Case D: the loop cannot turn, and says so -----------------------------


async def test_case_d_unsolvable_capability_asks_a_person_for_help(tmp_path):
    runtime = extension_runtime(tmp_path, "case-d.db")
    bootstrap_control(runtime)
    bootstrap_autonomy(runtime)
    bootstrap_orchestration(runtime)
    authorize(runtime)
    criteria = json.dumps(
        [
            {
                "type": "state_predicate",
                "entity": "project-a",
                "attribute": "deck_parsed",
                "operator": "==",
                "value": True,
                "work": {
                    "semantic_key": "parse-deck",
                    "objective": "PowerPointを解析する",
                    "work_type": "parse_deck",
                    "required_capabilities": [
                        {"name": "parse_powerpoint", "metadata": {"extension": POWERPOINT}}
                    ],
                },
            }
        ],
        separators=(",", ":"),
    )
    try:
        goal_id = await create_goal(runtime, criteria=criteria)
        work = runtime.work_requirement_store.for_goal(goal_id)[0]

        assert runtime.get_work_requirement(work.id).status is WorkStatus.BLOCKED_CAPABILITY
        assert runtime.control_store.get_goal(goal_id).status is GoalStatus.ACTIVE

        loop = get_goal_loop(runtime, goal_id)
        assert loop.stage is GoalLoopStage.HUMAN_REQUIRED
        assert loop.needs_human is True
        assert "parse_powerpoint" in loop.missing_capabilities
        assert loop.human_requests

        # The Goal keeps its Work; nothing was cancelled to make the stop tidy.
        assert runtime.work_requirement_store.for_goal(goal_id)[0].status is (
            WorkStatus.BLOCKED_CAPABILITY
        )
    finally:
        runtime.close()


async def test_a_blocked_acquisition_states_what_the_human_must_supply(tmp_path):
    runtime = extension_runtime(tmp_path, "intervention.db")
    bootstrap_control(runtime)
    bootstrap_autonomy(runtime)
    bootstrap_orchestration(runtime)
    authorize(runtime)
    criteria = json.dumps(
        [
            {
                "type": "state_predicate",
                "entity": "project-a",
                "attribute": "published",
                "operator": "==",
                "value": True,
                "work": {
                    "semantic_key": "publish",
                    "objective": "成果物を公開する",
                    "work_type": "publish_result",
                    "required_capabilities": [
                        {
                            "name": "publish_to_production",
                            "metadata": {
                                "extension": {"required_permissions": ["network.write"]}
                            },
                        }
                    ],
                },
            }
        ],
        separators=(",", ":"),
    )
    try:
        goal_id = await create_goal(runtime, criteria=criteria)
        work = runtime.work_requirement_store.for_goal(goal_id)[0]
        session = runtime.autonomy_store.sessions_for_work(work.id)[0]
        assert session.status.value == "BLOCKED"

        requests = runtime.event_store.by_type(HUMAN_INTERVENTION_REQUIRED)
        assert len(requests) == 1
        payload = requests[0].payload
        # What is being pursued, what stopped, what is missing, what was tried.
        assert payload["goal_id"] == str(goal_id)
        assert payload["goal_objective"] == "レビュー可能な状態にする"
        assert payload["project_id"] == project_id_for_goal(goal_id)
        assert payload["work_requirement_id"] == str(work.id)
        assert payload["work_status"] == WorkStatus.BLOCKED_CAPABILITY.value
        assert payload["missing_capabilities"] == ["publish_to_production"]
        assert "FORBIDDEN" in payload["reason"]
        assert any("policy=FORBIDDEN" in line for line in payload["tried"])
        assert "publish_to_production" in payload["next_action"]

        loop = get_goal_loop(runtime, goal_id)
        assert loop.stage is GoalLoopStage.HUMAN_REQUIRED
        assert loop.human_requests[0]["kind"] == "capability"
    finally:
        runtime.close()


async def test_goal_loop_reports_each_stage_without_writing(tmp_path):
    runtime = goal_runtime(tmp_path, "stages.db")
    register_worker(
        runtime, "prepare_review", world_updating_worker("project-a", "review_ready", True)
    )
    try:
        goal_id = await create_goal(runtime, criteria=review_ready_criteria())
        before = runtime.db.conn.total_changes

        loop = get_goal_loop(runtime, goal_id)
        assert runtime.db.conn.total_changes == before
        assert loop.project_id == project_id_for_goal(goal_id)
        assert loop.goal_status == GoalStatus.ACHIEVED.value
        assert loop.to_dict()["stage"] == GoalLoopStage.ACHIEVED.value

        await runtime.submit_event(
            Event("goal_paused", "test", {"goal_id": str(goal_id)})
        )
        runtime.control_store.update_goal_status(goal_id, GoalStatus.PAUSED)
        assert get_goal_loop(runtime, goal_id).stage is GoalLoopStage.PAUSED

        runtime.control_store.update_goal_status(goal_id, GoalStatus.CANCELLED)
        assert get_goal_loop(runtime, goal_id).stage is GoalLoopStage.CANCELLED

        assert get_goal_loop(runtime, uuid.uuid4()) is None
    finally:
        runtime.close()


async def test_a_goal_without_capability_or_work_is_still_planning(tmp_path):
    runtime = goal_runtime(tmp_path, "planning.db")
    try:
        goal_id = await create_goal(
            runtime,
            criteria=json.dumps(
                [
                    {
                        "type": "state_predicate",
                        "entity": "project-a",
                        "attribute": "ready",
                        "operator": "==",
                        "value": True,
                    }
                ],
                separators=(",", ":"),
            ),
        )
        loop = get_goal_loop(runtime, goal_id)
        assert loop.stage is GoalLoopStage.PLANNING
        assert loop.remaining_tasks == 0
        assert runtime.work_requirement_store.for_goal(goal_id) == []
    finally:
        runtime.close()
