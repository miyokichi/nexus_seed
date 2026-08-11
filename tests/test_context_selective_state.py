"""Selective state: only declared entities are compiled into the context."""

from __future__ import annotations

from nexus_seed.context.requirements import ContextRequirements, WorldStateReq
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessInstance,
    ProcessResult,
)
from nexus_seed.runtime.runtime import Runtime


async def _noop(ctx: ProcessContext) -> ProcessResult:
    return ctx.complete(output={})


async def test_only_requested_entities_are_included(tmp_path):
    runtime = Runtime(tmp_path / "sel.db")
    definition = ProcessDefinition(
        "sel",
        "1",
        "sel",
        context_requirements=ContextRequirements(
            world_state=WorldStateReq(entities=["D1_CD"])
        ),
    )
    runtime.register_process(definition, _noop)

    runtime.state_store.set("D1_CD", "target", 45)
    runtime.state_store.set("D1_HEIGHT", "target", 100)
    runtime.state_store.set("UNRELATED_X", "value", 999)

    instance = ProcessInstance(definition_name="sel", definition_version="1")
    runtime.process_store.save_instance(instance)

    view = runtime.context_compiler.compile(
        definition=definition, process_instance=instance
    )

    assert set(view.world_state.keys()) == {"D1_CD"}
    assert "UNRELATED_X" not in view.world_state
    assert "D1_HEIGHT" not in view.world_state
    assert view.get_state("D1_CD", "target") == 45
    # Provenance is retained on the item.
    assert view.get_state_entry("D1_CD", "target").history_id is not None
    runtime.close()
