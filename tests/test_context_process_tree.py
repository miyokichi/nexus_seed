"""Process-tree context: parent sees children/results, child sees parent."""

from __future__ import annotations

from nexus_seed.context.requirements import ContextRequirements, ProcessTreeReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
    SpawnSpec,
)
from nexus_seed.runtime.runtime import Runtime

PARENT = ProcessDefinition(
    "tree_parent",
    "1",
    "tree_parent",
    ("start_tree",),
    context_requirements=ContextRequirements(process_tree=ProcessTreeReq(children=True)),
)
CHILD = ProcessDefinition(
    "tree_child",
    "1",
    "tree_child",
    context_requirements=ContextRequirements(process_tree=ProcessTreeReq(parent=True)),
)


async def parent_handler(ctx: ProcessContext) -> ProcessResult:
    if ctx.resume_point is None:
        specs = [SpawnSpec("tree_child", "1", {"n": i}) for i in range(3)]
        return ctx.spawn_and_join(specs, mode="all", resume_point="done")
    return ctx.complete(output={"done": True})


async def child_handler(ctx: ProcessContext) -> ProcessResult:
    return ctx.complete(output={"n": ctx.instance.input["n"]})


async def test_parent_and_child_context(tmp_path):
    runtime = Runtime(tmp_path / "tree.db")
    runtime.register_process(PARENT, parent_handler)
    runtime.register_process(CHILD, child_handler)

    await runtime.submit_event(Event("start_tree", "test", {}))

    instances = runtime.process_store.all_instances()
    parent = [i for i in instances if i.definition_name == "tree_parent"][0]
    children = [i for i in instances if i.definition_name == "tree_child"]
    assert parent.status is ProcessStatus.COMPLETED
    assert len(children) == 3

    # Parent context includes children and their results.
    parent_view = runtime.context_compiler.compile(
        definition=PARENT, process_instance=runtime.process_store.get_instance(parent.id)
    )
    assert len(parent_view.child_processes) == 3
    assert len(parent_view.child_results) == 3
    assert {r["output"]["n"] for r in parent_view.child_results} == {0, 1, 2}

    # Child context includes the parent.
    child_view = runtime.context_compiler.compile(
        definition=CHILD, process_instance=children[0]
    )
    assert child_view.parent_process is not None
    assert child_view.parent_process.id == parent.id
    runtime.close()
