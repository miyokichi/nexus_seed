"""Retry: a transient failure goes RUNNING -> RETRY_WAIT -> RUNNABLE -> COMPLETED."""

from __future__ import annotations

from datetime import datetime, timezone

from nexus_seed.core.event import Event
from nexus_seed.core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
    RetryableError,
)
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


async def flaky_handler(ctx: ProcessContext) -> ProcessResult:
    """Fail (retryably) on the first attempt, succeed on the retry."""
    if ctx.instance.retry_count == 0:
        raise RetryableError("transient failure")
    ctx.state.set("job", "status", "ok", source_event=ctx.event.id)
    return ctx.complete(output={"attempt": ctx.instance.retry_count})


DEFINITION = ProcessDefinition("flaky", "1", "flaky", ("go",), max_retries=3)


async def test_retryable_failure_is_retried_then_succeeds(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "retry.db", clock=clock)
    runtime.register_process(DEFINITION, flaky_handler)

    await runtime.submit_event(Event("go", "test", {}))

    # First attempt failed retryably -> RETRY_WAIT scheduled for the future.
    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.RETRY_WAIT
    assert instance.retry_count == 1
    assert instance.next_retry_at is not None
    assert "transient" in instance.last_error

    # Before the backoff elapses, a tick does not resume it.
    await runtime.tick()
    assert runtime.process_store.get_instance(instance.id).status is ProcessStatus.RETRY_WAIT

    # After the backoff, the retry runs and succeeds.
    clock.advance(10)
    await runtime.tick()

    final = runtime.process_store.get_instance(instance.id)
    assert final.status is ProcessStatus.COMPLETED
    assert runtime.state_store.get("job", "status") == "ok"
    runtime.close()


async def test_non_retryable_failure_is_terminal(tmp_path):
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "retry2.db", clock=clock)

    async def always_fails(ctx: ProcessContext) -> ProcessResult:
        raise RuntimeError("hard failure")

    runtime.register_process(
        ProcessDefinition("boom", "1", "boom", ("go",), max_retries=3), always_fails
    )
    await runtime.submit_event(Event("go", "test", {}))

    # A plain exception is non-retryable -> FAILED immediately, no RETRY_WAIT.
    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.FAILED
    assert instance.retry_count == 0
    runtime.close()
