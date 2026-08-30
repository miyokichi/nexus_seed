"""Execute one Process activation and commit its effects atomically."""

from __future__ import annotations

import logging
from datetime import timedelta

from ..context.models import ContextSnapshot
from ..core.continuation import Continuation
from ..core.process import (
    HandlerRegistry,
    ProcessContext,
    ProcessInstance,
    ProcessResult,
    ProcessStatus,
    RetryableError,
    TimerSpec,
)
from ..core.state import StateView
from ..storage.activation_store import ActivationStore, activation_key
from ..storage.continuation_store import ContinuationStore
from ..storage.database import Database
from ..storage.event_store import EventStore
from ..storage.join_store import JoinRecord, JoinStore
from ..storage.process_store import ProcessStore
from ..storage.state_store import StateStore
from ..storage.timer_store import TimerRecord, TimerStore
from .clock import Clock

logger = logging.getLogger("nexus_seed.runtime.executor")


class Executor:
    """Run handlers and persist the small set of supported Runtime effects."""

    def __init__(
        self,
        db: Database,
        registry: HandlerRegistry,
        process_store: ProcessStore,
        continuation_store: ContinuationStore,
        state_store: StateStore,
        event_store: EventStore,
        timer_store: TimerStore,
        join_store: JoinStore,
        activation_store: ActivationStore,
        clock: Clock,
        *,
        compiler,
        context_snapshot_store,
        backends: dict,
        services,
        resource_store,
        adapters,
        ingress,
        project_orchestrator=None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.process_store = process_store
        self.continuation_store = continuation_store
        self.state_store = state_store
        self.event_store = event_store
        self.timer_store = timer_store
        self.join_store = join_store
        self.activation_store = activation_store
        self.clock = clock
        self.compiler = compiler
        self.context_snapshot_store = context_snapshot_store
        self.backends = backends
        self.services = services
        self.resource_store = resource_store
        self.adapters = adapters
        self.ingress = ingress
        self.project_orchestrator = project_orchestrator

    async def execute(self, instance: ProcessInstance) -> ProcessResult:
        """Run one activation of ``instance`` and return its result."""
        original_event_id = instance.pending_event_id
        key = activation_key(instance.id, original_event_id)
        if self.activation_store.exists(key):
            logger.error("activation %s already applied but still runnable", key)
            return self._fail(instance, "duplicate activation")

        definition = self.process_store.get_definition(
            instance.definition_name, instance.definition_version
        )
        if definition is None:
            return self._fail(instance, f"unknown definition {instance.definition_name}")
        try:
            handler = self.registry.get(definition.handler)
        except KeyError as exc:
            return self._fail(instance, str(exc))

        event = self.event_store.get(original_event_id) if original_event_id else None
        active_continuation = self.continuation_store.for_instance(instance.id)
        resume_point = active_continuation.resume_point if active_continuation else None
        saved_state = active_continuation.saved_process_state if active_continuation else {}

        instance.status = ProcessStatus.RUNNING
        self.process_store.save_instance(instance)
        try:
            view = self.compiler.compile(
                definition=definition,
                process_instance=instance,
                trigger_event=event,
                continuation=active_continuation,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("context compile failed for %s", definition.name)
            return self._fail(instance, f"context compile failed: {exc}", key=key)

        snapshot_id = self._save_snapshot(instance, event, view, key)
        ctx = ProcessContext(
            instance=instance,
            event=event,
            state=StateView(self.state_store, created_by_process_id=instance.id),
            view=view,
            resume_point=resume_point,
            saved_process_state=saved_state,
            services=self.services,
            backends=self.backends,
            adapters=self.adapters,
            ingress=self.ingress,
            project_orchestrator=self.project_orchestrator,
            context_snapshot_id=snapshot_id,
            activation_id=key,
            logger=logging.getLogger(f"nexus_seed.process.{definition.name}"),
        )
        try:
            result = await handler(ctx)
            if not isinstance(result, ProcessResult):
                raise TypeError("a Process handler must return ProcessResult")
        except RetryableError as exc:
            logger.warning("handler %s retryable error: %s", definition.handler, exc)
            result = ProcessResult(
                ProcessStatus.FAILED, output={"error": str(exc)}, retryable=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("handler %s raised", definition.handler)
            result = ProcessResult(ProcessStatus.FAILED, output={"error": str(exc)})

        if result.status is ProcessStatus.FAILED:
            return self._handle_failure(instance, key, result)
        return self._commit(instance, key, original_event_id, active_continuation, result)

    def _save_snapshot(self, instance, event, view, key):
        try:
            snapshot = ContextSnapshot(
                process_instance_id=instance.id,
                context_json=view.to_snapshot_dict(),
                trigger_event_id=event.id if event else None,
                activation_id=key,
            )
            self.context_snapshot_store.save(snapshot)
            return snapshot.id
        except Exception:  # noqa: BLE001
            logger.exception("failed to save context snapshot for %s", instance.id)
            return None

    def _commit(
        self, instance, key, original_event_id, active_continuation, result
    ) -> ProcessResult:
        try:
            with self.db.atomic():
                for resource in result.resources:
                    self.resource_store.save_resource(resource)
                for version in result.resource_versions:
                    self.resource_store.save_version(version)
                for representation in result.resource_representations:
                    self.resource_store.save_representation(representation)
                for change in result.state_changes:
                    self.state_store.set(
                        change.entity,
                        change.attribute,
                        change.value,
                        source_event=change.source_event,
                        observation_id=change.observation_id,
                        state_delta_id=change.state_delta_id,
                        created_by_process_id=change.created_by_process_id,
                        confidence=change.confidence,
                    )
                if active_continuation is not None:
                    self.continuation_store.delete(active_continuation.id)
                for continuation_id in result.continuations_to_delete:
                    self.continuation_store.delete(continuation_id)
                for timer_spec in result.timers_to_create:
                    self.timer_store.save(self._timer_record(timer_spec))
                for continuation in result.continuations_to_create:
                    self.continuation_store.save(continuation)
                for event in result.emitted_events:
                    self.event_store.append(event)

                self._apply_spawns_and_join(instance, result)
                instance.status = (
                    ProcessStatus.SUSPENDED
                    if result.status is ProcessStatus.SUSPENDED
                    else ProcessStatus.COMPLETED
                )
                if instance.status is ProcessStatus.COMPLETED and result.output is not None:
                    instance.local_state["output"] = result.output
                instance.last_error = None
                instance.pending_event_id = None
                instance.updated_at = self.clock.now()
                self.process_store.save_instance(instance)
                self.activation_store.record(key, instance.id, original_event_id)
        except Exception:  # noqa: BLE001
            logger.exception("failed to commit activation %s; rolled back", key)
            return self._fail(instance, "transaction failed")

        logger.info("instance %s -> %s", instance.id, instance.status.value)
        return result

    def _apply_spawns_and_join(self, instance, result) -> None:
        child_ids = []
        for spec in result.spawned_processes:
            child = ProcessInstance(
                definition_name=spec.definition_name,
                definition_version=spec.definition_version,
                input=dict(spec.input),
                parent_process_id=instance.id,
                priority=spec.priority,
            )
            child_definition = self.process_store.get_definition(
                spec.definition_name, spec.definition_version
            )
            if child_definition is not None:
                child.max_retries = child_definition.max_retries
            self.process_store.save_instance(child)
            child_ids.append(child.id)

        if result.join is not None:
            join = JoinRecord(
                parent_instance_id=instance.id,
                child_ids=child_ids,
                mode=result.join.mode,
                resume_point=result.join.resume_point,
                saved_process_state=result.join.saved_process_state,
            )
            self.join_store.save(join)
            self.continuation_store.save(
                Continuation(
                    process_instance_id=instance.id,
                    resume_point=result.join.resume_point,
                    waiting_for={"event_type": "join_satisfied", "join_id": str(join.id)},
                    saved_process_state=join.saved_process_state,
                )
            )

    def _timer_record(self, spec: TimerSpec) -> TimerRecord:
        fire_at = spec.fire_at or self.clock.now() + timedelta(seconds=spec.delay or 0)
        return TimerRecord(
            fire_at=fire_at,
            event_type="timer_fired",
            payload=spec.payload,
            id=spec.id,
        )

    def _handle_failure(self, instance, key, result) -> ProcessResult:
        error = (result.output or {}).get("error", "unknown error")
        if result.retryable and instance.retry_count < instance.max_retries:
            instance.retry_count += 1
            delay = result.retry_delay
            if delay is None:
                delay = float(2 ** (instance.retry_count - 1))
            instance.status = ProcessStatus.RETRY_WAIT
            instance.next_retry_at = self.clock.now() + timedelta(seconds=delay)
            instance.last_error = error
            instance.updated_at = self.clock.now()
            self.process_store.save_instance(instance)
            logger.info(
                "instance %s -> RETRY_WAIT (attempt %d/%d, +%.0fs)",
                instance.id,
                instance.retry_count,
                instance.max_retries,
                delay,
            )
            return result
        return self._fail(instance, error, key=key)

    def _fail(self, instance, error, *, key=None) -> ProcessResult:
        with self.db.atomic():
            instance.status = ProcessStatus.FAILED
            instance.last_error = str(error)
            instance.local_state["error"] = str(error)
            instance.pending_event_id = None
            instance.updated_at = self.clock.now()
            self.process_store.save_instance(instance)
            if key is not None:
                self.activation_store.record(key, instance.id, None)
        logger.error("instance %s failed: %s", instance.id, error)
        return ProcessResult(ProcessStatus.FAILED, output={"error": str(error)})
