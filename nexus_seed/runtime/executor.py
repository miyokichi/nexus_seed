"""Process Executor — runs one activation and commits its effects atomically.

The executor is pure mechanism.  It resolves the handler, builds the
:class:`ProcessContext`, awaits the handler, and applies the returned
:class:`ProcessResult` in a **single database transaction** (Phase 2A):

* apply all state changes, emitted events, continuation create/delete, spawned
  children and joins together — or roll them all back on error;
* transition the instance to COMPLETED / SUSPENDED / RETRY_WAIT / FAILED;
* record the activation so the same event cannot apply effects twice.

It does not route emitted events — that is the runtime's drain loop.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ..context.models import ContextSnapshot
from ..core.continuation import Continuation
from ..core.effects import EffectConflictError, check_conflicts
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
from ..storage.observation_store import ObservationStore
from ..storage.process_store import ProcessStore
from ..storage.state_delta_store import StateDeltaStore
from ..storage.state_store import StateStore
from ..storage.timer_store import TimerRecord, TimerStore
from ..storage.work_requirement_store import WorkRequirementStore
from .clock import Clock

logger = logging.getLogger("nexus_seed.runtime.executor")


class Executor:
    """Executes a single process activation and persists its outcome atomically."""

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
        observation_store: ObservationStore,
        state_delta_store: StateDeltaStore,
        work_requirement_store: WorkRequirementStore,
        clock: Clock,
        compiler=None,
        context_snapshot_store=None,
        proposal_store=None,
        llm_invocation_store=None,
        backends: dict | None = None,
        services: object | None = None,
        action_proposal_store=None,
        action_execution_store=None,
        action_decision_store=None,
        resource_store=None,
        adapters=None,
        ingress=None,
        capability_store=None,
        plan_store=None,
        decision_store=None,
        extension_store=None,
        construction_store=None,
        installation_store=None,
        installation_manager=None,
        autonomy_store=None,
        provider_registry=None,
    ) -> None:
        self.capability_store = capability_store
        self.plan_store = plan_store
        self.decision_store = decision_store
        self.extension_store = extension_store
        self.construction_store = construction_store
        self.installation_store = installation_store
        self.installation_manager = installation_manager
        self.autonomy_store = autonomy_store
        self.provider_registry = provider_registry
        self.db = db
        self.registry = registry
        self.process_store = process_store
        self.continuation_store = continuation_store
        self.state_store = state_store
        self.event_store = event_store
        self.timer_store = timer_store
        self.join_store = join_store
        self.activation_store = activation_store
        self.observation_store = observation_store
        self.state_delta_store = state_delta_store
        self.work_requirement_store = work_requirement_store
        self.clock = clock
        self.compiler = compiler
        self.context_snapshot_store = context_snapshot_store
        self.proposal_store = proposal_store
        self.llm_invocation_store = llm_invocation_store
        self.backends = backends if backends is not None else {}
        self.services = services
        self.action_proposal_store = action_proposal_store
        self.action_execution_store = action_execution_store
        self.action_decision_store = action_decision_store
        self.resource_store = resource_store
        self.adapters = adapters
        self.ingress = ingress

    async def execute(self, instance: ProcessInstance) -> ProcessResult:
        """Run one activation of ``instance`` and return its result."""
        original_event_id = instance.pending_event_id
        key = activation_key(instance.id, original_event_id)
        if self.activation_store.exists(key):
            # Should not happen: a committed activation advances status out of
            # RUNNABLE.  Fail loudly rather than risk re-applying / looping.
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

        event = (
            self.event_store.get(original_event_id) if original_event_id else None
        )
        active_continuation = self.continuation_store.for_instance(instance.id)
        resume_point = active_continuation.resume_point if active_continuation else None
        saved_state = (
            active_continuation.saved_process_state if active_continuation else {}
        )

        # RUNNING marker is committed on its own so crash recovery can see it.
        instance.status = ProcessStatus.RUNNING
        self.process_store.save_instance(instance)

        # Compile a fresh, read-only Context view from current Memory.  On
        # resume this recompiles against *current* state, never the suspend-time
        # snapshot (Invariant 13).  A compile failure means the handler does not
        # run (spec §49).
        try:
            view = self.compiler.compile(
                definition=definition,
                process_instance=instance,
                trigger_event=event,
                continuation=active_continuation,
            )
        except Exception as exc:  # noqa: BLE001 - surface, don't swallow
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
            context_snapshot_id=snapshot_id,
            activation_id=key,
            logger=logging.getLogger(f"nexus_seed.process.{definition.name}"),
        )

        try:
            if self.provider_registry is None:
                result = await handler(ctx)
            else:
                result = await self.provider_registry.execute(definition, ctx, handler)
        except RetryableError as exc:
            logger.warning("handler %s retryable error: %s", definition.handler, exc)
            result = ProcessResult(
                status=ProcessStatus.FAILED, output={"error": str(exc)}, retryable=True
            )
        except Exception as exc:  # noqa: BLE001 - surface, don't swallow
            logger.exception("handler %s raised", definition.handler)
            result = ProcessResult(
                status=ProcessStatus.FAILED, output={"error": str(exc)}, retryable=False
            )

        if result.status is ProcessStatus.FAILED:
            return self._handle_failure(instance, key, result)

        # Validate the batch *before* opening the transaction: two staged
        # writes disagreeing about one record must fail the activation, not be
        # resolved by whichever happened to be last in a list (Invariant 65).
        try:
            check_conflicts(result)
        except EffectConflictError as exc:
            logger.error("effect conflict in %s: %s", definition.name, exc)
            return self._fail(instance, f"effect conflict: {exc}", key=key, audit=result)

        return self._commit(instance, key, original_event_id, active_continuation, result)

    def _save_snapshot(self, instance, event, view, key):
        """Persist an audit snapshot of the compiled context; return its id."""
        if self.context_snapshot_store is None:
            return None
        try:
            snapshot = ContextSnapshot(
                process_instance_id=instance.id,
                context_json=view.to_snapshot_dict(),
                trigger_event_id=event.id if event else None,
                activation_id=key,
            )
            self.context_snapshot_store.save(snapshot)
            return snapshot.id
        except Exception:  # noqa: BLE001 - auditing must never break execution
            logger.exception("failed to save context snapshot for %s", instance.id)
            return None

    def _commit(
        self,
        instance: ProcessInstance,
        key: str,
        original_event_id,
        active_continuation,
        result: ProcessResult,
    ) -> ProcessResult:
        """Apply a successful (COMPLETED/SUSPENDED) result in one transaction."""
        try:
            with self.db.atomic():
                for observation in result.observations:
                    self.observation_store.save(observation)
                for delta in result.state_deltas:
                    self.state_delta_store.save(delta)
                for requirement in result.work_requirements:
                    self.work_requirement_store.save(requirement)
                for requirement_id, status in result.work_requirement_updates:
                    self.work_requirement_store.update_status(requirement_id, status)
                for requirement_id, status, missing, selected in result.work_matches:
                    self.work_requirement_store.record_match(
                        requirement_id,
                        status=status,
                        missing_capabilities=missing,
                        selected_definition=selected,
                    )
                if self.capability_store is not None:
                    for match in result.capability_matches:
                        self.capability_store.save_match(match)
                if self.plan_store is not None:
                    for plan, nodes, edges in result.plans_to_create:
                        self.plan_store.create(plan, nodes, edges)
                    for plan_id, status in result.plan_updates:
                        self.plan_store.update_status(plan_id, status)
                    for node_id, status, instance_id in result.plan_node_updates:
                        self.plan_store.update_node(
                            node_id, status, process_instance_id=instance_id
                        )
                if self.decision_store is not None:
                    for evaluation in result.plan_evaluations:
                        self.decision_store.save_evaluation(evaluation)
                    for proposal in result.plan_selection_proposals:
                        self.decision_store.save_proposal(proposal)
                    for proposal_id, status, reasons in (
                        result.plan_selection_proposal_updates
                    ):
                        self.decision_store.update_proposal_status(
                            proposal_id, status, reasons=reasons or None
                        )
                    for selection in result.plan_selections:
                        self.decision_store.save_selection(selection)
                        if selection.selected_plan_id is not None:
                            # The decision, the selected plan pointer and the
                            # plan/event transitions are one atomic activation
                            # (spec §72).  A crash cannot leave a selection
                            # whose WorkRequirement points somewhere else.
                            self.work_requirement_store.set_selected_plan(
                                selection.work_requirement_id,
                                selection.selected_plan_id,
                            )
                    for attempt in result.replan_attempts:
                        self.decision_store.save_replan_attempt(attempt)
                for requirement_id, attempt_number in result.replan_counts:
                    self.work_requirement_store.record_replan(
                        requirement_id, attempt_number
                    )
                if self.extension_store is not None:
                    # Gaps before proposals: a proposal references the gap it
                    # closes, and the two must land in one transaction so a
                    # crash cannot leave a proposal about nothing (spec §132).
                    for gap in result.capability_gaps:
                        self.extension_store.save_gap(gap)
                    for gap_id, status in result.capability_gap_updates:
                        self.extension_store.update_gap_status(gap_id, status)
                    for extension_proposal in result.extension_proposals:
                        self.extension_store.save_proposal(extension_proposal)
                    for proposal_id, status, reasons in result.extension_proposal_updates:
                        self.extension_store.update_proposal_status(
                            proposal_id, status, reasons=reasons or None
                        )
                    for extension_decision in result.extension_decisions:
                        self.extension_store.save_decision(extension_decision)
                if self.construction_store is not None:
                    for plan in result.construction_plans:
                        self.construction_store.save_plan(plan)
                    for plan_id, status in result.construction_plan_updates:
                        self.construction_store.update_plan_status(plan_id, status)
                    for step_id, status in result.construction_step_updates:
                        self.construction_store.update_step_status(step_id, status)
                    for workspace in result.sandbox_workspaces:
                        self.construction_store.save_workspace(workspace)
                    for workspace_id, status, closed_at in result.sandbox_workspace_updates:
                        self.construction_store.update_workspace_status(
                            workspace_id, status, closed_at=closed_at
                        )
                    for grant in result.construction_grants:
                        self.construction_store.save_grant(grant)
                    for grant_id, status in result.construction_grant_updates:
                        self.construction_store.update_grant_status(grant_id, status)
                    for check in result.verification_checks:
                        self.construction_store.save_check(check)
                    for construction_result in result.construction_results:
                        self.construction_store.save_result(construction_result)
                if self.installation_store is not None:
                    for plan in result.installation_plans:
                        self.installation_store.save_plan(plan)
                    for plan_id, status, reasons in result.installation_plan_updates:
                        self.installation_store.update_plan_status(
                            plan_id, status, reasons=reasons or None
                        )
                    for step_id, status in result.installation_step_updates:
                        self.installation_store.update_step_status(step_id, status)
                    for grant in result.installation_grants:
                        self.installation_store.save_grant(grant)
                    for grant_id, status, revoked_at in result.installation_grant_updates:
                        self.installation_store.update_grant_status(
                            grant_id, status, revoked_at=revoked_at
                        )
                    for check in result.installation_checks:
                        self.installation_store.save_check(check)
                    for installation_result in result.installation_results:
                        self.installation_store.save_result(installation_result)
                    for decision in result.installation_decisions:
                        self.installation_store.save_decision(decision)
                    for rollback in result.rollback_records:
                        self.installation_store.save_rollback(rollback)
                    for activation, definition, capabilities in result.installation_activations:
                        # Component identity, runnable definition and provider
                        # links are one atomic transition (Invariant 111).
                        self.process_store.upsert_definition(definition)
                        if self.provider_registry is not None:
                            # Phase 5E extends the same atomic publication:
                            # an installed internal definition is not runnable
                            # until its local execution provider exists.
                            self.provider_registry.ensure_internal_binding(definition)
                        for capability in capabilities:
                            persisted = self.capability_store.save(capability)
                            self.capability_store.link(
                                definition.name, definition.version, persisted.id
                            )
                        self.installation_store.save_activation(activation)
                        if self.installation_manager is not None and self.services is not None:
                            self.installation_manager.register_memory_component(
                                activation, self.services.get_extractors()
                            )
                if self.autonomy_store is not None:
                    for session in result.acquisition_sessions:
                        self.autonomy_store.save_session(session)
                    for subscriber in result.acquisition_subscribers:
                        self.autonomy_store.subscribe(subscriber)
                    for autonomy_decision in result.autonomy_decisions:
                        self.autonomy_store.save_decision(autonomy_decision)
                    for attempt in result.acquisition_attempts:
                        self.autonomy_store.save_attempt(attempt)
                self._persist_journals(result)
                if self.proposal_store is not None:
                    for proposal in result.proposals:
                        self.proposal_store.save(proposal)
                    for proposal_id, decision in result.proposal_updates:
                        self.proposal_store.update_decision(proposal_id, decision)
                if self.action_proposal_store is not None:
                    for action_proposal in result.action_proposals:
                        self.action_proposal_store.save(action_proposal)
                    for proposal_id, status in result.action_proposal_updates:
                        self.action_proposal_store.update_status(proposal_id, status)
                if self.action_decision_store is not None:
                    for decision_record in result.action_decisions:
                        self.action_decision_store.save(decision_record)
                if self.resource_store is not None:
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
                for cont_id in result.continuations_to_delete:
                    self.continuation_store.delete(cont_id)
                for process_id, status, output in result.process_instance_updates:
                    other = self.process_store.get_instance(process_id)
                    if other is not None and other.status is ProcessStatus.SUSPENDED:
                        other.status = ProcessStatus(status)
                        other.pending_event_id = None
                        if output is not None:
                            other.local_state["output"] = output
                        other.updated_at = self.clock.now()
                        self.process_store.save_instance(other)

                for timer_spec in result.timers_to_create:
                    self.timer_store.save(self._timer_record(timer_spec))

                for cont in result.continuations_to_create:
                    self.continuation_store.save(cont)

                for event in result.emitted_events:
                    self.event_store.append(event)

                self._apply_spawns_and_join(instance, result)

                if result.status is ProcessStatus.SUSPENDED:
                    instance.status = ProcessStatus.SUSPENDED
                else:
                    instance.status = ProcessStatus.COMPLETED
                    if result.output is not None:
                        instance.local_state["output"] = result.output
                instance.pending_event_id = None
                instance.updated_at = self.clock.now()
                self.process_store.save_instance(instance)

                self.activation_store.record(key, instance.id, original_event_id)
        except Exception:  # noqa: BLE001 - rollback already happened
            logger.exception("failed to commit activation %s; rolled back", key)
            return self._fail(instance, "transaction failed")

        logger.info("instance %s -> %s", instance.id, instance.status.value)
        return result

    def _persist_journals(self, result: ProcessResult) -> None:
        """Write the attempt journals (LLM invocations, action executions).

        Called from *both* the success and failure paths: a backend call that
        was made and failed is exactly the thing an audit needs to see
        (Invariant 26).  Must run inside the caller's transaction.
        """
        if self.llm_invocation_store is not None:
            for invocation in result.llm_invocations:
                self.llm_invocation_store.save(invocation)
        if self.action_execution_store is not None:
            for execution in result.action_executions:
                self.action_execution_store.save(execution)

    def _apply_spawns_and_join(
        self, instance: ProcessInstance, result: ProcessResult
    ) -> None:
        """Create spawned children (and a join record if the parent is joining)."""
        child_ids = []
        for spec in result.spawned_processes:
            child = ProcessInstance(
                definition_name=spec.definition_name,
                definition_version=spec.definition_version,
                status=ProcessStatus.RUNNABLE,
                input=dict(spec.input),
                parent_process_id=instance.id,
                priority=spec.priority,
                work_key=spec.work_key,
                work_requirement_id=spec.work_requirement_id,
                plan_id=spec.plan_id,
                plan_node_id=spec.plan_node_id,
            )
            child_def = self.process_store.get_definition(
                spec.definition_name, spec.definition_version
            )
            if child_def is not None:
                child.max_retries = child_def.max_retries
            self.process_store.save_instance(child)
            child_ids.append(child.id)
            # Bind the plan position to the instance now filling it, in the
            # same transaction that created it — so a crash cannot leave a node
            # believing nothing was spawned when something was.
            if spec.plan_node_id is not None and self.plan_store is not None:
                self.plan_store.update_node(
                    spec.plan_node_id,
                    "RUNNING",
                    process_instance_id=child.id,
                )

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
                    waiting_for={
                        "event_type": "join_satisfied",
                        "join_id": str(join.id),
                    },
                    saved_process_state=join.saved_process_state,
                )
            )

    def _timer_record(self, spec: TimerSpec) -> TimerRecord:
        if spec.fire_at is not None:
            fire_at = spec.fire_at
        else:
            fire_at = self.clock.now() + timedelta(seconds=spec.delay or 0)
        return TimerRecord(
            fire_at=fire_at,
            event_type="timer_fired",
            payload=spec.payload,
            id=spec.id,
        )

    def _handle_failure(
        self, instance: ProcessInstance, key: str, result: ProcessResult
    ) -> ProcessResult:
        """Route a FAILED result to RETRY_WAIT or terminal FAILED."""
        error = (result.output or {}).get("error", "unknown error")
        if result.retryable and instance.retry_count < instance.max_retries:
            instance.retry_count += 1
            delay = (
                result.retry_delay
                if result.retry_delay is not None
                else float(2 ** (instance.retry_count - 1))
            )
            with self.db.atomic():
                self._persist_journals(result)
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
        return self._fail(instance, error, key=key, audit=result)

    def _fail(
        self,
        instance: ProcessInstance,
        error: object,
        *,
        key: str | None = None,
        audit: ProcessResult | None = None,
    ) -> ProcessResult:
        with self.db.atomic():
            if audit is not None:
                self._persist_journals(audit)
            instance.status = ProcessStatus.FAILED
            instance.last_error = str(error)
            instance.local_state["error"] = str(error)
            instance.pending_event_id = None
            instance.updated_at = self.clock.now()
            self.process_store.save_instance(instance)
            if key is not None:
                self.activation_store.record(key, instance.id, None)
        logger.error("instance %s failed: %s", instance.id, error)
        return ProcessResult(status=ProcessStatus.FAILED, output={"error": str(error)})
