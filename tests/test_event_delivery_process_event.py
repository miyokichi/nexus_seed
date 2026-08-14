"""AT12 (spec §63, §9): events a Process emits are covered too.

Every event-producing path gets the same guarantee, because the obligation is
created by ``EventStore.append`` rather than by each caller.  A process that
commits its result and then dies has still handed its events to the system.
"""

from __future__ import annotations

from delivery_helpers import RECORDED, instances_named, manual_runtime, ping, status_of

from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.runtime.runtime import Runtime

EMITTER = ProcessDefinition(
    name="emitter",
    version="1",
    handler="emitter",
    trigger_event_types=("ping",),
    context_requirements=ContextRequirements(include_trigger_event=True),
)

CONSUMER = ProcessDefinition(
    name="consumer",
    version="1",
    handler="consumer",
    trigger_event_types=("pong",),
    context_requirements=ContextRequirements(include_trigger_event=True),
)


async def emitter(ctx):
    return ctx.complete(
        output={"emitted": True},
        emitted_events=[ctx.new_event("pong", {"from": str(ctx.instance.id)})],
    )


async def consumer(ctx):
    RECORDED.append({"event_id": str(ctx.event.id), "type": ctx.event.type})
    return ctx.complete(output={"consumed": True})


def build(runtime):
    RECORDED.clear()
    runtime.register_process(EMITTER, emitter)
    runtime.register_process(CONSUMER, consumer)
    return runtime


async def test_an_emitted_event_gets_its_own_obligation(tmp_path):
    runtime = build(Runtime(tmp_path / "p.db"))

    await runtime.submit_event(ping())

    pong = runtime.event_store.by_type("pong")[0]
    assert runtime.get_event_delivery(pong.id) is not None
    assert status_of(runtime, pong.id) == "DELIVERED"
    assert len(RECORDED) == 1
    runtime.close()


async def test_an_emitted_event_survives_a_crash_before_it_is_routed(tmp_path):
    """AT12: the process committed; the runtime died before the next sweep."""
    db_path = tmp_path / "p.db"

    runtime = Runtime(db_path)
    runtime.register_process(EMITTER, emitter)  # no consumer registered yet
    RECORDED.clear()

    # Run the emitter without letting the drain reach its output: execute the
    # activation directly, exactly as the loop would, then stop.
    runtime.event_store.append(ping())
    runtime.dispatch_pending_events()
    instance = runtime.scheduler.next_runnable()
    await runtime.executor.execute(instance)

    pong = runtime.event_store.by_type("pong")[0]
    assert status_of(runtime, pong.id) == "PENDING"
    runtime.close()

    # --- restart, now with something that cares about pong ---
    runtime2 = build(Runtime(db_path))
    assert runtime2.get_pending_event_delivery_count() == 1

    await runtime2.run_pending()

    assert status_of(runtime2, pong.id) == "DELIVERED"
    assert len(RECORDED) == 1
    assert RECORDED[0]["event_id"] == str(pong.id)
    runtime2.close()


async def test_a_chain_of_emitted_events_all_get_obligations(tmp_path):
    runtime = build(Runtime(tmp_path / "p.db"))
    await runtime.submit_event(ping())

    events = runtime.event_store.all()
    deliveries = runtime.get_event_deliveries()
    assert {d.event_id for d in deliveries} == {e.id for e in events}
    assert all(d.status.value == "DELIVERED" for d in deliveries)
    runtime.close()


async def test_a_failed_activation_does_not_leave_its_events_behind(tmp_path):
    """A rolled-back activation persists no events, so it owes no deliveries."""

    async def explodes(ctx):
        raise RuntimeError("handler died")

    runtime = Runtime(tmp_path / "p.db")
    runtime.register_process(
        ProcessDefinition(
            name="explodes",
            version="1",
            handler="explodes",
            trigger_event_types=("ping",),
        ),
        explodes,
    )

    event = ping()
    await runtime.submit_event(event)

    assert runtime.process_store.all_instances()[0].status is ProcessStatus.FAILED
    assert [e.id for e in runtime.event_store.all()] == [event.id]
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()


async def test_a_spawned_child_and_its_join_event_are_covered(tmp_path):
    """Join events go through the same append, so they are covered too."""
    from nexus_seed.core.process import SpawnSpec

    PARENT = ProcessDefinition(
        name="parent",
        version="1",
        handler="parent",
        trigger_event_types=("ping",),
        context_requirements=ContextRequirements(include_trigger_event=True),
    )
    CHILD = ProcessDefinition(name="child", version="1", handler="child")

    async def parent(ctx):
        if ctx.resume_point == "joined":
            return ctx.complete(output={"joined": True})
        return ctx.spawn_and_join(
            [SpawnSpec(definition_name="child", definition_version="1")],
            mode="all",
            resume_point="joined",
        )

    async def child(ctx):
        return ctx.complete(output={"done": True})

    runtime = Runtime(tmp_path / "p.db")
    runtime.register_process(PARENT, parent)
    runtime.register_process(CHILD, child)

    await runtime.submit_event(ping())

    parent_instance = instances_named(runtime, "parent")[0]
    assert parent_instance.status is ProcessStatus.COMPLETED
    deliveries = runtime.get_event_deliveries()
    assert {d.event_id for d in deliveries} == {e.id for e in runtime.event_store.all()}
    assert runtime.get_pending_event_delivery_count() == 0
    runtime.close()
