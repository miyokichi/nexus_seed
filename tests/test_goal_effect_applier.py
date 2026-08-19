"""Goal records are committed by the Control Plane, not by the Runtime.

`ProcessResult.goals` / `goal_updates` and `ctx.record_goal(...)` are unchanged
— handlers keep the API they already use (effects spec §77). What moved is
*who applies them*: the executor no longer knows Goal exists, so removing the
Control Plane is deleting a module rather than editing the Runtime.
"""

from __future__ import annotations

import inspect

from nexus_seed.control.models import Goal, GoalStatus
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessContext, ProcessDefinition, ProcessResult
from nexus_seed.processes.control import apply_goal_effects, bootstrap_control
from nexus_seed.runtime import executor as executor_module
from nexus_seed.runtime.runtime import Runtime

MAKE_GOAL = ProcessDefinition("make_goal", "1", "make_goal", ("make_goal",))
FAIL_AFTER_GOAL = ProcessDefinition("boom", "1", "boom", ("boom",))


async def make_goal(ctx: ProcessContext) -> ProcessResult:
    goal = Goal(title="T", objective="O", owner_identity_id="operator")
    ctx.record_goal(goal)
    return ctx.complete(output={"goal_id": str(goal.id)})


def test_the_executor_no_longer_mentions_goals():
    source = inspect.getsource(executor_module)
    assert "save_goal" not in source
    assert "update_goal_status" not in source
    assert "result.goals" not in source


async def test_goals_are_applied_when_the_control_plane_registered_its_applier(tmp_path):
    runtime = Runtime(tmp_path / "on.db")
    bootstrap_control(runtime)
    runtime.register_process(MAKE_GOAL, make_goal)

    await runtime.submit_event(Event("make_goal", "test", {}))

    [goal] = runtime.control_store.goals()
    assert goal.title == "T"
    runtime.close()


async def test_without_the_applier_the_activation_still_succeeds(tmp_path):
    """No Control Plane, no Goal writes — and no crash in the Runtime."""
    runtime = Runtime(tmp_path / "off.db", control_enabled=False)
    runtime.register_process(MAKE_GOAL, make_goal)  # bootstrap_control not called

    await runtime.submit_event(Event("make_goal", "test", {}))

    instance = runtime.process_store.all_instances()[0]
    assert instance.status.value == "COMPLETED"
    assert runtime.control_store.goals() == []
    assert runtime.result_appliers == []
    runtime.close()


async def test_goal_status_updates_go_through_the_applier(tmp_path):
    runtime = Runtime(tmp_path / "status.db")
    bootstrap_control(runtime)
    goal = Goal(title="T", objective="O", owner_identity_id="operator")
    runtime.control_store.save_goal(goal)

    async def achieve(ctx: ProcessContext) -> ProcessResult:
        ctx.update_goal(goal.id, GoalStatus.ACHIEVED)
        return ctx.complete(output={})

    runtime.register_process(ProcessDefinition("ach", "1", "ach", ("ach",)), achieve)
    await runtime.submit_event(Event("ach", "test", {}))

    assert runtime.control_store.get_goal(goal.id).status is GoalStatus.ACHIEVED
    runtime.close()


async def test_the_applier_runs_inside_the_activation_transaction(tmp_path):
    """A failed commit must roll the Goal back with everything else."""
    runtime = Runtime(tmp_path / "atomic.db")
    bootstrap_control(runtime)

    async def goal_then_fail(ctx: ProcessContext) -> ProcessResult:
        ctx.record_goal(Goal(title="rolled back", objective="O", owner_identity_id="op"))
        ctx.state.set("x", "y", 1)
        return ctx.complete(output={})

    runtime.register_process(FAIL_AFTER_GOAL, goal_then_fail)

    original = runtime.process_store.save_instance

    def failing_save(instance):
        if instance.status.value == "COMPLETED":
            raise RuntimeError("injected mid-transaction failure")
        return original(instance)

    runtime.process_store.save_instance = failing_save
    await runtime.submit_event(Event("boom", "test", {}))
    runtime.process_store.save_instance = original

    # Neither the Goal nor the state change survived the rollback.
    assert runtime.control_store.goals() == []
    assert runtime.state_store.get("x", "y") is None
    runtime.close()


def test_registering_the_same_name_twice_does_not_double_apply(tmp_path):
    runtime = Runtime(tmp_path / "twice.db")
    bootstrap_control(runtime)
    bootstrap_control(runtime)  # a re-bootstrapped runtime

    names = [name for name, _ in runtime.result_appliers]
    assert names.count("control.goals") == 1
    runtime.close()


def test_an_applier_is_reachable_from_the_executor_after_bootstrap(tmp_path):
    """The executor holds the list by reference; a copy would stay empty."""
    runtime = Runtime(tmp_path / "ref.db")
    assert runtime.executor.result_appliers == []

    runtime.register_result_applier("demo", apply_goal_effects(runtime))

    assert [name for name, _ in runtime.executor.result_appliers] == ["demo"]
    runtime.close()
