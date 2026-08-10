"""Demo process: resistance analysis with suspend/resume.

This is an ordinary :class:`ProcessDefinition` + handler — nothing about it is
"special".  It demonstrates the whole Phase 1 loop:

1. A ``process_parameter_changed`` event triggers the process.
2. It updates world State (``<parameter>.target``).
3. It needs a wafer measurement that does not exist yet, so it **suspends**,
   saving a Continuation that waits for ``measurement_completed`` for that wafer.
4. Later a ``measurement_completed`` event resumes it; it records the
   measurement, **completes**, and emits ``resistance_analysis_completed``.
"""

from __future__ import annotations

from ..core.process import ProcessContext, ProcessDefinition, ProcessResult

#: Wafer this demo analysis depends on.
DEFAULT_WAFER = "W03"

#: The process definition registered with the runtime.
DEFINITION = ProcessDefinition(
    name="resistance_analysis",
    version="1",
    handler="resistance_analysis",
    trigger_event_types=("process_parameter_changed",),
)


async def resistance_analysis(ctx: ProcessContext) -> ProcessResult:
    """Handle both the initial activation and the resumed comparison."""
    if ctx.resume_point is None:
        return _begin(ctx)
    if ctx.resume_point == "compare_resistance":
        return _compare(ctx)
    return ctx.fail(f"unknown resume_point: {ctx.resume_point!r}")


def _begin(ctx: ProcessContext) -> ProcessResult:
    """First activation: update target, then wait for a measurement."""
    assert ctx.event is not None
    payload = ctx.event.payload
    parameter = payload["parameter"]
    new_target = payload["new"]
    wafer = payload.get("wafer", DEFAULT_WAFER)

    ctx.state.set(parameter, "target", new_target, source_event=ctx.event.id)
    ctx.logger.info("set %s.target = %s", parameter, new_target)

    measurement = ctx.state.get(f"measurement_{wafer}", "resistance")
    if measurement is None:
        ctx.logger.info("no measurement for %s; suspending", wafer)
        return ctx.suspend(
            resume_point="compare_resistance",
            waiting_for={"event_type": "measurement_completed", "wafer": wafer},
            saved_process_state={
                "parameter": parameter,
                "target": new_target,
                "wafer": wafer,
            },
        )
    return _finish(ctx, parameter, new_target, wafer, measurement)


def _compare(ctx: ProcessContext) -> ProcessResult:
    """Resumed activation: record the measurement and complete."""
    assert ctx.event is not None
    saved = ctx.saved_process_state
    wafer = saved["wafer"]
    resistance = ctx.event.payload["resistance"]
    ctx.state.set(f"measurement_{wafer}", "resistance", resistance, source_event=ctx.event.id)
    ctx.logger.info("recorded %s resistance = %s", wafer, resistance)
    return _finish(ctx, saved["parameter"], saved["target"], wafer, resistance)


def _finish(ctx, parameter, target, wafer, resistance) -> ProcessResult:
    """Emit the completion event and complete the process."""
    completed = ctx.new_event(
        "resistance_analysis_completed",
        {
            "parameter": parameter,
            "target": target,
            "wafer": wafer,
            "resistance": resistance,
        },
    )
    return ctx.complete(
        output={"wafer": wafer, "resistance": resistance, "target": target},
        emitted_events=[completed],
    )


def bootstrap(runtime) -> None:
    """Register the demo definition + handler on ``runtime`` (idempotent)."""
    runtime.register_process(DEFINITION, resistance_analysis)
