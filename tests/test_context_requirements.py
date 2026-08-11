"""ContextRequirements: serialization round-trip and persistence on a definition."""

from __future__ import annotations

from nexus_seed.context.requirements import (
    ContextRequirements,
    ContinuationReq,
    EntityAttributes,
    EventsReq,
    ProcessTreeReq,
    WorkReq,
    WorldStateReq,
)
from nexus_seed.core.process import ProcessContext, ProcessDefinition, ProcessResult
from nexus_seed.runtime.runtime import Runtime


async def _noop(ctx: ProcessContext) -> ProcessResult:
    return ctx.complete(output={})


def test_to_from_dict_roundtrip():
    reqs = ContextRequirements(
        include_trigger_event=True,
        world_state=WorldStateReq(
            entities=["D1_CD"],
            entity_attributes=[EntityAttributes("D1_CD", ["target", "variation"])],
            include_work_entities=True,
        ),
        events=EventsReq(recent=5, related_entities=["D1_CD"]),
        work=WorkReq(current=True, related=True),
        process_tree=ProcessTreeReq(parent=True, children=True),
        continuation=ContinuationReq(include=True),
    )
    back = ContextRequirements.from_dict(reqs.to_dict())

    assert back.include_trigger_event is True
    assert back.world_state.entities == ["D1_CD"]
    assert back.world_state.entity_attributes[0].attributes == ["target", "variation"]
    assert back.world_state.include_work_entities is True
    assert back.events.recent == 5 and back.events.related_entities == ["D1_CD"]
    assert back.work.current and back.work.related
    assert back.process_tree.parent and back.process_tree.children
    assert back.continuation.include


def test_none_roundtrip():
    assert ContextRequirements.from_dict(None) is None
    assert ContextRequirements.from_dict({}) is None


def test_requirements_persist_on_definition(tmp_path):
    db_path = tmp_path / "reqs.db"
    runtime = Runtime(db_path)
    definition = ProcessDefinition(
        "p",
        "1",
        "p",
        context_requirements=ContextRequirements(
            world_state=WorldStateReq(entities=["D1_CD"])
        ),
    )
    runtime.register_process(definition, _noop)
    runtime.close()

    # Reload from SQLite only.
    runtime2 = Runtime(db_path)
    loaded = runtime2.process_store.get_definition("p", "1")
    assert loaded.context_requirements is not None
    assert loaded.context_requirements.world_state.entities == ["D1_CD"]
    runtime2.close()
