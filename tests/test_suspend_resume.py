"""The most important test: suspend -> runtime restart -> resume -> complete.

This encodes the Phase 1 acceptance condition.  It is *not* enough to keep the
process object alive in memory and resume it: the first Runtime is fully closed
and discarded, and a second Runtime is rebuilt from the same SQLite file before
the resuming event arrives.
"""

from __future__ import annotations

import uuid

from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessContext, ProcessDefinition, ProcessResult, ProcessStatus
from nexus_seed.runtime.runtime import Runtime


DEFINITION = ProcessDefinition(
    name="restart_safe_process",
    version="1",
    handler="restart_safe_process",
    trigger_event_types=("process_parameter_changed",),
)


async def restart_safe_process(ctx: ProcessContext) -> ProcessResult:
    """A minimal state -> suspend -> resume Process using only core primitives."""
    assert ctx.event is not None
    if ctx.resume_point is None:
        payload = ctx.event.payload
        ctx.state.set(payload["parameter"], "target", payload["new"], source_event=ctx.event.id)
        return ctx.suspend(
            resume_point="compare_resistance",
            waiting_for={"event_type": "measurement_completed", "wafer": payload["wafer"]},
            saved_process_state={
                "parameter": payload["parameter"],
                "target": payload["new"],
                "wafer": payload["wafer"],
            },
        )
    wafer = ctx.saved_process_state["wafer"]
    resistance = ctx.event.payload["resistance"]
    ctx.state.set(f"measurement_{wafer}", "resistance", resistance, source_event=ctx.event.id)
    return ctx.complete(
        output={"wafer": wafer, "resistance": resistance},
        emitted_events=[
            ctx.new_event(
                "resistance_analysis_completed",
                {"wafer": wafer, "resistance": resistance},
            )
        ],
    )


def bootstrap(runtime: Runtime) -> None:
    runtime.register_process(DEFINITION, restart_safe_process)


async def test_suspend_resume_across_runtime_restart(tmp_path):
    db_path = tmp_path / "nexus.db"
    correlation = uuid.uuid4()

    # --- Runtime instance #1 -------------------------------------------------
    runtime = Runtime(db_path)
    bootstrap(runtime)

    param_event = Event(
        type="process_parameter_changed",
        source="world",
        payload={"parameter": "D1_CD", "old": 48, "new": 45, "wafer": "W03"},
        correlation_id=correlation,
    )
    await runtime.submit_event(param_event)

    # Event persisted.
    assert runtime.event_store.get(param_event.id) is not None
    # State changed.
    assert runtime.state_store.get("D1_CD", "target") == 45
    # Exactly one instance, and it is SUSPENDED.
    instances = runtime.process_store.all_instances()
    assert len(instances) == 1
    instance_id = instances[0].id
    assert instances[0].status is ProcessStatus.SUSPENDED
    # Continuation persisted with the expected waiting condition.
    continuations = runtime.continuation_store.all()
    assert len(continuations) == 1
    assert continuations[0].resume_point == "compare_resistance"
    assert continuations[0].waiting_for == {
        "event_type": "measurement_completed",
        "wafer": "W03",
    }

    # --- Step 4: destroy runtime #1; only SQLite remains ---------------------
    runtime.close()
    del runtime

    # --- Runtime instance #2, rebuilt from the same database -----------------
    runtime2 = Runtime(db_path)
    bootstrap(runtime2)

    # Process state survived the restart.
    recovered = runtime2.process_store.get_instance(instance_id)
    assert recovered is not None
    assert recovered.status is ProcessStatus.SUSPENDED
    assert runtime2.state_store.get("D1_CD", "target") == 45
    assert len(runtime2.continuation_store.all()) == 1

    # --- Step 5-7: the matching event arrives; process resumes and completes -
    measurement_event = Event(
        type="measurement_completed",
        source="metrology",
        payload={"wafer": "W03", "resistance": 123.4},
        correlation_id=correlation,
    )
    produced = await runtime2.submit_event(measurement_event)

    final = runtime2.process_store.get_instance(instance_id)
    assert final.status is ProcessStatus.COMPLETED
    # Continuation consumed.
    assert runtime2.continuation_store.all() == []
    # Measurement recorded in world state.
    assert runtime2.state_store.get("measurement_W03", "resistance") == 123.4
    # Completion event emitted, with the causal chain intact.
    completions = [e for e in produced if e.type == "resistance_analysis_completed"]
    assert len(completions) == 1
    assert completions[0].causation_id == measurement_event.id
    assert completions[0].correlation_id == correlation
    # And it is persisted in the append-only log.
    assert runtime2.event_store.get(completions[0].id) is not None

    runtime2.close()


async def test_non_matching_event_does_not_resume(tmp_path):
    db_path = tmp_path / "nexus.db"
    runtime = Runtime(db_path)
    bootstrap(runtime)

    await runtime.submit_event(
        Event(
            "process_parameter_changed",
            "world",
            {"parameter": "D1_CD", "old": 48, "new": 45, "wafer": "W03"},
        )
    )
    instance_id = runtime.process_store.all_instances()[0].id

    # Wrong wafer: must not resume.
    await runtime.submit_event(
        Event("measurement_completed", "metrology", {"wafer": "W07", "resistance": 99.9})
    )
    assert runtime.process_store.get_instance(instance_id).status is ProcessStatus.SUSPENDED
    assert len(runtime.continuation_store.all()) == 1
    runtime.close()
