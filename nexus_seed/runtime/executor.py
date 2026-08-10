"""Process Executor — runs one activation of one process instance.

The executor is pure mechanism.  It resolves the handler, builds the
:class:`ProcessContext`, awaits the handler, and applies the returned
:class:`ProcessResult` to persistent state:

* transition the instance to COMPLETED / SUSPENDED / FAILED,
* persist a new continuation on suspend and remove a consumed one on resume,
* clear the pending activation.

It does **not** route emitted events — that is the runtime's drain loop.
"""

from __future__ import annotations

import logging

from ..core.context import build_context
from ..core.process import (
    HandlerRegistry,
    ProcessContext,
    ProcessInstance,
    ProcessResult,
    ProcessStatus,
)
from ..storage.continuation_store import ContinuationStore
from ..storage.event_store import EventStore
from ..storage.process_store import ProcessStore
from ..storage.state_store import StateStore

logger = logging.getLogger("nexus_seed.runtime.executor")


class Executor:
    """Executes a single process activation and persists its outcome."""

    def __init__(
        self,
        registry: HandlerRegistry,
        process_store: ProcessStore,
        continuation_store: ContinuationStore,
        state_store: StateStore,
        event_store: EventStore,
    ) -> None:
        self.registry = registry
        self.process_store = process_store
        self.continuation_store = continuation_store
        self.state_store = state_store
        self.event_store = event_store

    async def execute(self, instance: ProcessInstance) -> ProcessResult:
        """Run one activation of ``instance`` and return its result."""
        definition = self.process_store.get_definition(
            instance.definition_name, instance.definition_version
        )
        if definition is None:
            return self._fail(instance, f"unknown definition {instance.definition_name}")

        try:
            handler = self.registry.get(definition.handler)
        except KeyError as exc:
            return self._fail(instance, str(exc))

        # Determine activation mode from persisted state (survives restart).
        event = (
            self.event_store.get(instance.pending_event_id)
            if instance.pending_event_id
            else None
        )
        active_continuation = self.continuation_store.for_instance(instance.id)
        resume_point = active_continuation.resume_point if active_continuation else None
        saved_state = (
            active_continuation.saved_process_state if active_continuation else {}
        )

        instance.status = ProcessStatus.RUNNING
        self.process_store.save_instance(instance)

        correlation_id = None
        if event is not None:
            correlation_id = event.correlation_id or event.id
        context = build_context(
            instance, self.state_store, self.event_store, correlation_id=correlation_id
        )
        ctx = ProcessContext(
            instance=instance,
            event=event,
            state=self.state_store,
            context=context,
            resume_point=resume_point,
            saved_process_state=saved_state,
            logger=logging.getLogger(f"nexus_seed.process.{definition.name}"),
        )

        try:
            result = await handler(ctx)
        except Exception as exc:  # noqa: BLE001 - surface, don't swallow
            logger.exception("handler %s raised", definition.handler)
            return self._fail(instance, exc)

        return self._apply(instance, active_continuation, result)

    def _apply(
        self,
        instance: ProcessInstance,
        active_continuation,
        result: ProcessResult,
    ) -> ProcessResult:
        # A resumed activation consumes its continuation regardless of outcome.
        if active_continuation is not None:
            self.continuation_store.delete(active_continuation.id)

        if result.status is ProcessStatus.SUSPENDED:
            if result.continuation is None:
                return self._fail(instance, "suspended without a continuation")
            self.continuation_store.save(result.continuation)
            instance.status = ProcessStatus.SUSPENDED
        elif result.status is ProcessStatus.COMPLETED:
            instance.status = ProcessStatus.COMPLETED
            if result.output is not None:
                instance.local_state["output"] = result.output
        elif result.status is ProcessStatus.FAILED:
            instance.status = ProcessStatus.FAILED
            if result.output is not None:
                instance.local_state.update(result.output)
        else:  # pragma: no cover - defensive
            return self._fail(instance, f"invalid result status {result.status}")

        instance.pending_event_id = None
        self.process_store.save_instance(instance)
        logger.info("instance %s -> %s", instance.id, instance.status.value)
        return result

    def _fail(self, instance: ProcessInstance, error: object) -> ProcessResult:
        instance.status = ProcessStatus.FAILED
        instance.local_state["error"] = str(error)
        instance.pending_event_id = None
        self.process_store.save_instance(instance)
        logger.error("instance %s failed: %s", instance.id, error)
        return ProcessResult(status=ProcessStatus.FAILED, output={"error": str(error)})
