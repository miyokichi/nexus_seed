"""Fresh-resume context (the most important test): resume sees CURRENT state.

A process suspended while D1_CD.target == 45 must, on resume after the value
changes to 43, compile a Context showing 43 — never the suspend-time 45.
"""

from __future__ import annotations

from nexus_seed.context.requirements import ContextRequirements, WorldStateReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
)
from nexus_seed.runtime.runtime import Runtime

DEFINITION = ProcessDefinition(
    "watcher",
    "1",
    "watcher",
    ("start_watch",),
    context_requirements=ContextRequirements(
        world_state=WorldStateReq(entities=["D1_CD"])
    ),
)


async def watcher(ctx: ProcessContext) -> ProcessResult:
    target = ctx.view.get_state("D1_CD", "target")
    if ctx.resume_point is None:
        return ctx.suspend(resume_point="after", waiting_for={"event_type": "resume_now"})
    return ctx.complete(output={"observed_target": target})


async def test_resume_sees_current_not_suspended_state(tmp_path):
    runtime = Runtime(tmp_path / "fresh.db")
    runtime.register_process(DEFINITION, watcher)

    runtime.state_store.set("D1_CD", "target", 45)
    await runtime.submit_event(Event("start_watch", "test", {}))

    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.SUSPENDED

    # The first activation compiled 45.
    snapshot = runtime.get_latest_context_snapshot(instance.id)
    assert snapshot.context_json["world_state"]["D1_CD"]["target"]["value"] == 45

    # The world changes while the process is suspended.
    runtime.state_store.set("D1_CD", "target", 43)

    # Resume: the context is recompiled fresh and shows 43.
    await runtime.submit_event(Event("resume_now", "test", {}))

    final = runtime.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.COMPLETED
    assert final.local_state["output"]["observed_target"] == 43
    assert final.local_state["output"]["observed_target"] != 45
    runtime.close()
