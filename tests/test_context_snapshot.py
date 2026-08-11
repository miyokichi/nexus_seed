"""Context snapshot audit: the snapshot preserves what was seen at the time."""

from __future__ import annotations

from nexus_seed.context.requirements import ContextRequirements, WorldStateReq
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessContext, ProcessDefinition, ProcessResult
from nexus_seed.runtime.runtime import Runtime

DEFINITION = ProcessDefinition(
    "auditor",
    "1",
    "auditor",
    ("audit",),
    context_requirements=ContextRequirements(
        world_state=WorldStateReq(entities=["D1_CD"])
    ),
)


async def auditor(ctx: ProcessContext) -> ProcessResult:
    return ctx.complete(output={"seen": ctx.view.get_state("D1_CD", "target")})


async def test_snapshot_is_frozen_while_current_moves_on(tmp_path):
    runtime = Runtime(tmp_path / "snap.db")
    runtime.register_process(DEFINITION, auditor)

    runtime.state_store.set("D1_CD", "target", 45)
    await runtime.submit_event(Event("audit", "test", {}))
    instance = runtime.process_store.all_instances()[0]

    # The snapshot recorded what the activation saw (45).
    snapshot = runtime.get_latest_context_snapshot(instance.id)
    assert snapshot is not None
    assert snapshot.context_json["world_state"]["D1_CD"]["target"]["value"] == 45
    assert snapshot.activation_id is not None

    # The world changes afterwards.
    runtime.state_store.set("D1_CD", "target", 43)

    # The snapshot is unchanged (it is an audit record, not the truth)...
    snapshot_again = runtime.get_latest_context_snapshot(instance.id)
    assert snapshot_again.context_json["world_state"]["D1_CD"]["target"]["value"] == 45

    # ...while a fresh compile reflects the current world (43).
    fresh = runtime.context_compiler.compile(
        definition=DEFINITION, process_instance=instance
    )
    assert fresh.get_state("D1_CD", "target") == 43
    runtime.close()
