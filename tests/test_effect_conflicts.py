"""AT24 + AT25 + AT26 (spec §78–§82, §121): staged effects must not contradict.

Phase 4A shipped a bug of exactly this shape: one effect set a work status and
another, staged moments later in the same handler, silently overwrote it.  The
lists were applied in order and the last one won.

Two writes to the same record in one activation are either the same intention
stated twice — safe to collapse — or a genuine ambiguity the runtime has no
business resolving by list order (Invariant 65).
"""

from __future__ import annotations

import uuid

import pytest
from planning_helpers import planning_runtime

from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.effects import (
    EffectConflictError,
    check_conflicts,
    normalize_status_updates,
)
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessResult, ProcessStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus


# --- normalization in isolation -------------------------------------------


def test_identical_updates_collapse():
    """Spec §82: the same intention stated twice is not a conflict."""
    record = uuid.uuid4()
    assert normalize_status_updates(
        [(record, "SPAWNED"), (record, "SPAWNED")], label="work"
    ) == [(record, "SPAWNED")]


def test_different_updates_to_one_record_are_refused():
    record = uuid.uuid4()
    with pytest.raises(EffectConflictError) as exc:
        normalize_status_updates(
            [(record, "BLOCKED_CAPABILITY"), (record, "SPAWNED")], label="work requirement"
        )
    assert "conflicting work requirement updates" in str(exc.value)
    assert str(record) in str(exc.value)


def test_updates_to_different_records_are_left_alone():
    a, b = uuid.uuid4(), uuid.uuid4()
    updates = [(a, "SPAWNED"), (b, "SATISFIED")]
    assert normalize_status_updates(updates, label="work") == updates


def test_order_is_preserved_when_collapsing():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert normalize_status_updates(
        [(a, "X"), (b, "Y"), (a, "X")], label="work"
    ) == [(a, "X"), (b, "Y")]


# --- the whole result ------------------------------------------------------


def result_with(**kwargs) -> ProcessResult:
    return ProcessResult(status=ProcessStatus.COMPLETED, **kwargs)


def test_check_conflicts_covers_every_status_list():
    record = uuid.uuid4()
    for field in (
        "work_requirement_updates",
        "proposal_updates",
        "action_proposal_updates",
        "plan_updates",
    ):
        result = result_with(**{field: [(record, "A"), (record, "B")]})
        with pytest.raises(EffectConflictError):
            check_conflicts(result)


def test_a_selection_alongside_a_status_is_not_a_conflict():
    """The 4A case, correctly allowed: they are different facts."""
    record = uuid.uuid4()
    result = result_with(
        work_requirement_updates=[(record, "MATCHED")],
        work_matches=[(record, None, [], ("analyzer", "1"))],
    )
    check_conflicts(result)  # does not raise
    assert result.work_requirement_updates == [(record, "MATCHED")]


def test_a_match_that_contradicts_a_status_is_refused():
    """The 4A bug, now caught instead of silently applied."""
    record = uuid.uuid4()
    result = result_with(
        work_requirement_updates=[(record, "SPAWNED")],
        work_matches=[(record, "BLOCKED_CAPABILITY", ["a"], None)],
    )
    with pytest.raises(EffectConflictError) as exc:
        check_conflicts(result)
    assert "SPAWNED" in str(exc.value) and "BLOCKED_CAPABILITY" in str(exc.value)


def test_a_match_agreeing_with_the_status_is_fine():
    record = uuid.uuid4()
    result = result_with(
        work_requirement_updates=[(record, "BLOCKED_CAPABILITY")],
        work_matches=[(record, "BLOCKED_CAPABILITY", ["a"], None)],
    )
    check_conflicts(result)


def test_plan_node_updates_are_normalized_too():
    node = uuid.uuid4()
    instance = uuid.uuid4()
    result = result_with(
        plan_node_updates=[(node, "RUNNING", instance), (node, "RUNNING", instance)]
    )
    check_conflicts(result)
    assert result.plan_node_updates == [(node, "RUNNING", instance)]

    conflicting = result_with(
        plan_node_updates=[(node, "COMPLETED", None), (node, "FAILED", None)]
    )
    with pytest.raises(EffectConflictError):
        check_conflicts(conflicting)


# --- through the runtime ---------------------------------------------------


CONFLICTING = ProcessDefinition(
    name="conflicting",
    version="1",
    handler="conflicting",
    trigger_event_types=("go",),
    context_requirements=ContextRequirements(include_trigger_event=True),
)


async def test_a_conflicting_activation_fails_without_committing(tmp_path):
    """AT24: no silent last-write-wins, and no partial commit."""
    runtime = Runtime(tmp_path / "c.db")
    requirement = WorkRequirement(work_type="demo", work_key="w1")
    runtime.work_requirement_store.save(requirement)

    async def conflicting(ctx):
        ctx.mark_work(requirement.id, WorkStatus.SPAWNED)
        ctx.mark_work(requirement.id, WorkStatus.CANCELLED)
        return ctx.complete(
            output={"done": True}, emitted_events=[ctx.new_event("side_effect", {})]
        )

    runtime.register_process(CONFLICTING, conflicting)
    await runtime.submit_event(Event("go", "test", {}))

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "conflicting"
    ][0]
    assert instance.status is ProcessStatus.FAILED
    assert "effect conflict" in instance.last_error
    # Nothing from the activation landed.
    assert runtime.work_requirement_store.get(requirement.id).status is WorkStatus.EXPECTED
    assert runtime.event_store.by_type("side_effect") == []
    runtime.close()


async def test_a_duplicate_but_compatible_update_commits_normally(tmp_path):
    """AT25."""
    runtime = Runtime(tmp_path / "c.db")
    requirement = WorkRequirement(work_type="demo", work_key="w1")
    runtime.work_requirement_store.save(requirement)

    async def duplicating(ctx):
        ctx.mark_work(requirement.id, WorkStatus.SATISFIED)
        ctx.mark_work(requirement.id, WorkStatus.SATISFIED)
        return ctx.complete(output={"done": True})

    runtime.register_process(CONFLICTING, duplicating)
    await runtime.submit_event(Event("go", "test", {}))

    instance = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "conflicting"
    ][0]
    assert instance.status is ProcessStatus.COMPLETED
    assert runtime.work_requirement_store.get(requirement.id).status is WorkStatus.SATISFIED
    runtime.close()


async def test_the_real_pipeline_stages_no_conflicts(tmp_path):
    """AT26: the shipped processes stay within the rule."""
    from planning_helpers import make_work, offer_work, register_chain

    runtime = planning_runtime(tmp_path)
    register_chain(runtime)
    await offer_work(runtime, make_work(runtime))

    failed = [
        i
        for i in runtime.process_store.all_instances()
        if i.status is ProcessStatus.FAILED
    ]
    assert failed == []
    runtime.close()
