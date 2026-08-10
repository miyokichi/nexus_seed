"""Spawn / join: a parent spawns children and resumes once they all finish."""

from __future__ import annotations

from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
    SpawnSpec,
)
from nexus_seed.runtime.runtime import Runtime


async def parent_handler(ctx: ProcessContext) -> ProcessResult:
    """Spawn three children, wait for all, then sum their outputs."""
    if ctx.resume_point is None:
        specs = [SpawnSpec("child", "1", {"n": i}) for i in range(3)]
        return ctx.spawn_and_join(specs, mode="all", resume_point="collect")
    children = ctx.event.payload["children"]
    total = sum(c["output"]["n"] for c in children)
    ctx.state.set("parent", "total", total, source_event=ctx.event.id)
    return ctx.complete(output={"total": total, "count": len(children)})


async def child_handler(ctx: ProcessContext) -> ProcessResult:
    """Return the number handed to the child."""
    return ctx.complete(output={"n": ctx.instance.input["n"]})


PARENT = ProcessDefinition("parent", "1", "parent", ("start",))
CHILD = ProcessDefinition("child", "1", "child")


async def test_parent_waits_for_all_children(tmp_path):
    runtime = Runtime(tmp_path / "join.db")
    runtime.register_process(PARENT, parent_handler)
    runtime.register_process(CHILD, child_handler)

    await runtime.submit_event(Event("start", "test", {}))

    instances = runtime.process_store.all_instances()
    parents = [i for i in instances if i.definition_name == "parent"]
    children = [i for i in instances if i.definition_name == "child"]

    assert len(parents) == 1
    assert len(children) == 3
    assert all(c.status is ProcessStatus.COMPLETED for c in children)
    assert all(c.parent_process_id == parents[0].id for c in children)

    parent = runtime.process_store.get_instance(parents[0].id)
    assert parent.status is ProcessStatus.COMPLETED
    assert parent.local_state["output"]["count"] == 3
    assert runtime.state_store.get("parent", "total") == 0 + 1 + 2

    # The join was consumed; the parent has no dangling continuation.
    assert runtime.continuation_store.for_instance(parent.id) is None
    runtime.close()


async def test_parent_waits_for_any_child(tmp_path):
    runtime = Runtime(tmp_path / "join_any.db")

    async def any_parent(ctx: ProcessContext) -> ProcessResult:
        if ctx.resume_point is None:
            specs = [SpawnSpec("child", "1", {"n": i}) for i in range(3)]
            return ctx.spawn_and_join(specs, mode="any", resume_point="collect")
        return ctx.complete(output={"got": len(ctx.event.payload["children"])})

    runtime.register_process(
        ProcessDefinition("anyp", "1", "anyp", ("start",)), any_parent
    )
    runtime.register_process(CHILD, child_handler)

    await runtime.submit_event(Event("start", "test", {}))

    parent = [
        i for i in runtime.process_store.all_instances() if i.definition_name == "anyp"
    ][0]
    parent = runtime.process_store.get_instance(parent.id)
    assert parent.status is ProcessStatus.COMPLETED
    assert parent.local_state["output"]["got"] >= 1
    runtime.close()
