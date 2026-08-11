"""Continuation context: a suspended process's continuation is compiled in."""

from __future__ import annotations

from nexus_seed.context.requirements import ContextRequirements, ContinuationReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
)
from nexus_seed.runtime.runtime import Runtime

DEFINITION = ProcessDefinition(
    "susp",
    "1",
    "susp",
    ("start",),
    context_requirements=ContextRequirements(continuation=ContinuationReq(include=True)),
)


async def suspender(ctx: ProcessContext) -> ProcessResult:
    if ctx.resume_point is None:
        return ctx.suspend(resume_point="later", waiting_for={"event_type": "never"})
    return ctx.complete(output={})


async def test_continuation_included_when_requested(tmp_path):
    runtime = Runtime(tmp_path / "cont.db")
    runtime.register_process(DEFINITION, suspender)

    await runtime.submit_event(Event("start", "test", {}))
    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.SUSPENDED

    stored = runtime.continuation_store.for_instance(instance.id)
    view = runtime.context_compiler.compile(
        definition=DEFINITION, process_instance=instance, continuation=stored
    )
    assert view.continuation is not None
    assert view.continuation.id == stored.id
    assert view.continuation.resume_point == "later"

    # Without the passed continuation, the compiler still finds it in storage.
    view2 = runtime.context_compiler.compile(
        definition=DEFINITION, process_instance=instance
    )
    assert view2.continuation is not None
    assert view2.continuation.id == stored.id
    runtime.close()
