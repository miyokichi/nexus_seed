"""Provider registry, deterministic selection and durable delegation routing."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from ..core.process import ProcessStatus
from .adapters import ProviderUnavailableBeforeStart
from .models import (
    LOCAL_PROVIDER_ID,
    DelegationRequest,
    DelegationResult,
    DelegationStatus,
    ExecutionProvider,
    ProviderBinding,
    ProviderHealth,
    ProviderInvocation,
    ProviderInvocationStatus,
    ProviderKind,
    ProviderSelection,
    ProviderStatus,
    local_provider,
)


logger = logging.getLogger(__name__)


class ProviderUnavailableError(RuntimeError):
    """A semantic ProcessDefinition has no operational executor."""


class DelegationValidationError(ValueError):
    """An external result exceeded or violated the ProcessDefinition contract."""


class ProviderSelector:
    """Choose one eligible binding with a stable, fully deterministic order."""

    def select(self, candidates: list[tuple[ProviderBinding, ExecutionProvider]]):
        """Return the highest ranked candidate, or ``None`` when unavailable."""
        return sorted(candidates, key=self._rank)[0] if candidates else None

    @staticmethod
    def _rank(item):
        binding, provider = item
        return (
            -binding.priority,
            -provider.priority,
            -provider.trust_level,
            provider.estimated_cost if provider.estimated_cost is not None else float("inf"),
            provider.estimated_latency if provider.estimated_latency is not None else float("inf"),
            float(provider.metadata.get("current_load", 0) or 0),
            str(provider.id),
        )


class ProviderRegistry:
    """Operational self-model kept separate from CapabilityRegistry."""

    def __init__(self, store, capability_registry=None) -> None:
        self.store = store
        self.capabilities = capability_registry
        self.selector = ProviderSelector()
        self.adapters: dict[str, object] = {}
        self.store.save_provider(local_provider())

    def register_adapter(self, name: str, adapter) -> None:
        """Attach an application protocol implementation for this runtime."""
        self.adapters[name] = adapter

    def register_provider(self, provider: ExecutionProvider, adapter=None) -> ExecutionProvider:
        """Persist a provider and optionally attach its adapter."""
        saved = self.store.save_provider(provider)
        if adapter is not None:
            self.register_adapter(saved.adapter_name, adapter)
        return saved

    def register_binding(self, binding: ProviderBinding) -> ProviderBinding:
        """Persist a binding after checking that its provider exists."""
        if self.store.get_provider(binding.provider_id) is None:
            raise ValueError(f"provider {binding.provider_id} is not registered")
        return self.store.save_binding(binding)

    def ensure_internal_binding(self, definition) -> ProviderBinding:
        """Idempotently migrate a local definition to ``local_runtime``."""
        required = tuple((definition.metadata or {}).get("permissions") or ())
        return self.store.save_binding(
            ProviderBinding(
                process_definition_name=definition.name,
                process_definition_version=definition.version,
                provider_id=LOCAL_PROVIDER_ID,
                required_permissions=required,
                metadata={"automatic_internal_migration": True},
            )
        )

    def disable_provider(self, provider_id) -> None:
        """Disable future selection without deleting history or active work."""
        self.store.update_provider(provider_id, status=ProviderStatus.DISABLED)

    def get_bindings_for_process(self, name: str, version: str) -> list[ProviderBinding]:
        """Return every binding for one ProcessDefinition version."""
        return self.store.bindings_for(name, version)

    def get_available_providers(self) -> list[ExecutionProvider]:
        """Return providers currently eligible at the operational layer."""
        return [p for p in self.store.all_providers() if self._provider_available(p)]

    def provider_health(self, provider_id=None):
        """Return one health value or aggregate counts by health status."""
        if provider_id is not None:
            provider = self.store.get_provider(provider_id)
            return provider.health if provider else None
        providers = self.store.all_providers()
        return {
            health.value: sum(p.health is health for p in providers)
            for health in ProviderHealth
        }

    def eligible_bindings(
        self, definition, *, excluded_provider_ids: set[uuid.UUID] | None = None
    ) -> list[tuple[ProviderBinding, ExecutionProvider]]:
        """Filter bindings by status, adapter, exclusion and permissions."""
        excluded = excluded_provider_ids or set()
        eligible = []
        for binding in self.store.bindings_for(definition.name, definition.version):
            provider = self.store.get_provider(binding.provider_id)
            if provider is None or provider.id in excluded or not binding.enabled:
                continue
            if not self._provider_available(provider):
                continue
            if provider.kind is not ProviderKind.INTERNAL:
                if provider.adapter_name not in self.adapters:
                    continue
                if not set(binding.required_permissions).issubset(provider.declared_permissions):
                    continue
            eligible.append((binding, provider))
        return eligible

    def has_eligible_provider(self, definition) -> bool:
        """Whether a definition has at least one usable execution path."""
        return bool(self.eligible_bindings(definition))

    def _provider_available(self, provider: ExecutionProvider) -> bool:
        if not provider.operational:
            return False
        return provider.kind is ProviderKind.INTERNAL or provider.adapter_name in self.adapters

    async def cancel_invocation(self, invocation_id) -> bool:
        """Mark one external delegation cancelled and tell the provider if it can.

        Remote cancellation is best effort: NEXUS SEED owns durability, so the
        journal is updated whether or not the external executor cooperates.
        """
        invocation = self.store.get_invocation(invocation_id)
        if invocation is None:
            return False
        if invocation.status in {
            ProviderInvocationStatus.COMPLETED,
            ProviderInvocationStatus.FAILED,
            ProviderInvocationStatus.CANCELLED,
        }:
            return False
        provider = self.store.get_provider(invocation.provider_id)
        adapter = self.adapters.get(provider.adapter_name) if provider else None
        cancel = getattr(adapter, "cancel", None)
        if cancel is not None and invocation.external_run_id:
            try:
                await cancel(invocation.external_run_id)
            except Exception:
                logger.warning(
                    "provider cancel failed for invocation %s",
                    invocation.id,
                    exc_info=True,
                )
        invocation.status = ProviderInvocationStatus.CANCELLED
        invocation.completed_at = datetime.now(invocation.started_at.tzinfo)
        self.store.save_invocation(invocation)
        return True

    async def execute(self, definition, ctx, internal_handler):
        """Select and execute Internal/Skill/Agent under one Process lifecycle."""

        if ctx.resume_point == "await_provider_result":
            return self._resume_external(definition, ctx)
        return await self._execute_new(definition, ctx, internal_handler, excluded=set())

    async def _execute_new(self, definition, ctx, internal_handler, *, excluded):
        eligible = self.eligible_bindings(definition, excluded_provider_ids=excluded)
        work = (
            ctx.services.get_work_requirement(ctx.instance.work_requirement_id)
            if ctx.services is not None and ctx.instance.work_requirement_id is not None
            else None
        )
        eligible, constraint_reasons = self._apply_work_constraints(eligible, work)
        selected = self.selector.select(eligible)
        selection = ProviderSelection(
            process_instance_id=ctx.instance.id,
            activation_id=ctx.activation_id or "",
            process_definition_name=definition.name,
            process_definition_version=definition.version,
            provider_id=selected[1].id if selected else None,
            provider_binding_id=selected[0].id if selected else None,
            eligible_provider_ids=[provider.id for _, provider in eligible],
            reasons=[
                *constraint_reasons,
                "selected by binding priority, provider priority, trust, cost, latency, load, id"
                if selected else "no eligible execution provider"
            ],
        )
        self.store.save_selection(selection)
        if selected is None:
            if work is not None:
                ctx.mark_work(work.id, "BLOCKED_PROVIDER")
                return ctx.complete(
                    output={"blocked": True, "reason": "provider constraints unavailable"},
                    emitted_events=[ctx.new_event(
                        "provider_missing",
                        {"work_requirement_id": str(work.id), "reasons": constraint_reasons},
                    )],
                )
            raise ProviderUnavailableError(
                f"no eligible provider for {definition.name}:v{definition.version}"
            )
        binding, provider = selected
        if provider.kind is ProviderKind.INTERNAL:
            return await internal_handler(ctx)

        invocation_key = f"{ctx.activation_id}:{provider.id}"
        existing = self.store.invocation_for_key(invocation_key)
        if existing is not None:
            return self._from_existing(definition, ctx, existing)

        request = self._request(definition, ctx, binding, provider, invocation_key, work)
        invocation = ProviderInvocation(
            id=request.invocation_id,
            provider_id=provider.id,
            provider_binding_id=binding.id,
            process_instance_id=ctx.instance.id,
            plan_node_id=ctx.instance.plan_node_id,
            request_snapshot=request.to_dict(),
            idempotency_key=invocation_key,
        )
        self.store.save_invocation(invocation)
        invocation.status = ProviderInvocationStatus.RUNNING
        self.store.save_invocation(invocation)
        adapter = self.adapters[provider.adapter_name]
        try:
            delegated = await adapter.execute(request)
        except ProviderUnavailableBeforeStart as exc:
            invocation.status = ProviderInvocationStatus.FAILED
            invocation.error = str(exc)
            invocation.completed_at = datetime.now(invocation.started_at.tzinfo)
            self.store.save_invocation(invocation)
            self.store.update_provider(provider.id, health=ProviderHealth.UNAVAILABLE)
            excluded.add(provider.id)
            return await self._execute_new(
                definition, ctx, internal_handler, excluded=excluded
            )
        except Exception as exc:
            invocation.status = ProviderInvocationStatus.FAILED
            invocation.error = str(exc)
            invocation.completed_at = datetime.now(invocation.started_at.tzinfo)
            self.store.save_invocation(invocation)
            self.store.update_provider(provider.id, health=ProviderHealth.DEGRADED)
            return ctx.fail(f"external provider execution failed: {exc}")
        return self._accept_delegation(definition, ctx, invocation, delegated)

    @staticmethod
    def _apply_work_constraints(candidates, work):
        """Apply human hard constraints/preferences without widening safety."""
        if work is None:
            return candidates, []
        constraints = work.constraints or {}
        allowed = {str(value) for value in constraints.get("allowed_providers", ())}
        forbidden = {str(value) for value in constraints.get("forbidden_providers", ())}
        directive = work.provider_directive or {}
        kind = str(directive.get("kind") or "").upper()
        named = str(directive.get("provider") or "")
        if kind == "FORBID" and named:
            forbidden.add(named)

        def matches(provider, values):
            identities = {str(provider.id), provider.name, provider.kind.value, provider.adapter_name}
            return bool(identities & values)

        filtered = []
        reasons = []
        for binding, provider in candidates:
            if allowed and not matches(provider, allowed):
                continue
            if forbidden and matches(provider, forbidden):
                continue
            if constraints.get("network_forbidden") and provider.kind is not ProviderKind.INTERNAL:
                continue
            if constraints.get("cloud_forbidden") and bool(provider.metadata.get("cloud")):
                continue
            if constraints.get("production_write_forbidden") and any(
                str(permission).startswith("production.")
                for permission in binding.required_permissions
            ):
                continue
            filtered.append((binding, provider))
        if kind == "REQUIRE" and named:
            filtered = [item for item in filtered if matches(item[1], {named})]
            reasons.append(f"human REQUIRE provider {named}")
        elif kind == "PREFER" and named:
            preferred = [item for item in filtered if matches(item[1], {named})]
            if preferred:
                filtered = preferred
                reasons.append(f"human PREFER provider {named} applied")
            else:
                reasons.append(f"preferred provider {named} unavailable; fallback allowed")
        if constraints:
            reasons.append("human work constraints narrowed provider eligibility")
        return filtered, reasons

    def _from_existing(self, definition, ctx, invocation):
        if invocation.status is ProviderInvocationStatus.COMPLETED:
            delegated = DelegationResult.from_dict(
                invocation.result_snapshot or {}, invocation_id=invocation.id
            )
            return self._complete(definition, ctx, invocation, delegated)
        if invocation.status in {
            ProviderInvocationStatus.PENDING,
            ProviderInvocationStatus.RUNNING,
            ProviderInvocationStatus.WAITING_EXTERNAL,
        }:
            return self._suspend(ctx, invocation)
        return ctx.fail(invocation.error or "provider invocation did not complete")

    def _accept_delegation(self, definition, ctx, invocation, delegated):
        if delegated.invocation_id != invocation.id:
            return ctx.fail("provider returned a different invocation_id")
        if delegated.status is DelegationStatus.PENDING:
            invocation.status = ProviderInvocationStatus.WAITING_EXTERNAL
            invocation.external_run_id = delegated.external_run_id
            invocation.result_snapshot = delegated.to_dict()
            self.store.save_invocation(invocation)
            return self._suspend(ctx, invocation)
        if delegated.status is DelegationStatus.COMPLETED:
            return self._complete(definition, ctx, invocation, delegated)
        invocation.status = ProviderInvocationStatus.FAILED
        invocation.error = delegated.error or delegated.status.value
        invocation.result_snapshot = delegated.to_dict()
        invocation.completed_at = datetime.now(invocation.started_at.tzinfo)
        self.store.save_invocation(invocation)
        self.store.update_provider(invocation.provider_id, health=ProviderHealth.DEGRADED)
        return ctx.fail(f"external provider {delegated.status.value}: {invocation.error}")

    def _resume_external(self, definition, ctx):
        raw_id = ctx.saved_process_state.get("provider_invocation_id")
        invocation = self.store.get_invocation(raw_id) if raw_id else None
        if invocation is None:
            return ctx.fail("provider invocation not found on resume")
        if invocation.status is ProviderInvocationStatus.COMPLETED:
            return self._from_existing(definition, ctx, invocation)
        payload = ctx.event.payload if ctx.event else {}
        if str(payload.get("invocation_id")) != str(invocation.id):
            return ctx.fail("provider_result invocation_id does not match continuation")
        try:
            delegated = DelegationResult.from_dict(payload, invocation_id=invocation.id)
        except (KeyError, ValueError, TypeError) as exc:
            return ctx.fail(f"invalid provider_result: {exc}")
        return self._accept_delegation(definition, ctx, invocation, delegated)

    def _complete(self, definition, ctx, invocation, delegated):
        try:
            self._validate_result(definition, delegated)
        except DelegationValidationError as exc:
            invocation.status = ProviderInvocationStatus.FAILED
            invocation.error = str(exc)
            invocation.result_snapshot = delegated.to_dict()
            invocation.completed_at = datetime.now(invocation.started_at.tzinfo)
            self.store.save_invocation(invocation)
            self.store.update_provider(
                invocation.provider_id, health=ProviderHealth.DEGRADED
            )
            return ctx.fail(f"invalid external output: {exc}")
        invocation.status = ProviderInvocationStatus.COMPLETED
        invocation.result_snapshot = delegated.to_dict()
        invocation.external_run_id = delegated.external_run_id
        invocation.completed_at = datetime.now(invocation.started_at.tzinfo)
        self.store.save_invocation(invocation)
        self.store.update_provider(invocation.provider_id, health=ProviderHealth.HEALTHY)
        emitted = []
        if ctx.instance.work_requirement_id is not None and ctx.instance.plan_id is None:
            ctx.satisfy_work()
            emitted.append(
                ctx.new_event(
                    "work_satisfied",
                    {
                        "work_requirement_id": str(ctx.instance.work_requirement_id),
                        "work_key": ctx.instance.work_key,
                    },
                )
            )
        emitted.append(
            ctx.new_event(
                "provider_execution_completed",
                {
                    "provider_invocation_id": str(invocation.id),
                    "process_instance_id": str(ctx.instance.id),
                },
            )
        )
        return ctx.complete(
            output={
                "outputs": delegated.typed_outputs,
                "artifacts": delegated.artifacts,
                "proposals": delegated.proposals,
                "provider_invocation_id": str(invocation.id),
            },
            emitted_events=emitted,
        )

    @staticmethod
    def _suspend(ctx, invocation):
        return ctx.suspend(
            resume_point="await_provider_result",
            waiting_for={
                "event_type": "provider_result",
                "invocation_id": str(invocation.id),
            },
            saved_process_state={"provider_invocation_id": str(invocation.id)},
        )

    def _request(self, definition, ctx, binding, provider, key, work=None):
        capabilities = self._capabilities_for(definition)
        relevant_context = ctx.view.to_snapshot_dict() if ctx.view else {}
        metadata = (definition.metadata or {})
        correlation = {"provider_id": str(provider.id)}
        if ctx.instance.work_requirement_id is not None:
            correlation["work_requirement_id"] = str(ctx.instance.work_requirement_id)
        if work is not None and work.project:
            correlation["project_id"] = str(work.project)
        if work is not None and work.goal_id is not None:
            correlation["goal_id"] = str(work.goal_id)
        return DelegationRequest(
            invocation_id=uuid.uuid4(),
            process_instance_id=ctx.instance.id,
            plan_node_id=ctx.instance.plan_node_id,
            process_definition={
                "name": definition.name,
                "version": definition.version,
                "description": metadata.get("description", ""),
                # The semantic output contract, so an external executor can be
                # told what shape to return instead of guessing.
                "output_types": sorted(self._declared_output_types(definition)),
                "output_schema": metadata.get("output_schema") or {},
                "instructions": metadata.get("instructions", ""),
            },
            required_capabilities=[c.name for c in capabilities],
            # What *this* need is for, when the need says so.  A Skill is a
            # reusable procedure and its definition can only state a generic
            # objective; the WorkRequirement is the one that knows the job.
            objective=(
                (work.objective if work is not None and work.objective else None)
                or metadata.get("objective", definition.name)
            ),
            typed_inputs=dict(ctx.instance.input.get("inputs") or ctx.instance.input),
            relevant_context=relevant_context,
            constraints=[
                *list((binding.metadata or {}).get("constraints") or []),
                *list(((work.constraints if work else {}) or {}).get("additional") or []),
            ],
            allowed_permissions=list(binding.required_permissions),
            idempotency_key=key,
            metadata=correlation,
        )

    def _declared_output_types(self, definition) -> set[str]:
        """Return every output type this definition and its capabilities declare."""
        declared = set((definition.metadata or {}).get("output_types") or ())
        for capability in self._capabilities_for(definition):
            declared.update(capability.output_types)
        return declared

    def _capabilities_for(self, definition):
        if self.capabilities is None:
            return []
        return self.capabilities.get_capabilities_for_process(
            definition.name, definition.version
        )

    def _validate_result(self, definition, result: DelegationResult) -> None:
        forbidden = {"world_state_update", "state_delta", "actions", "action_instruction"}
        if forbidden.intersection(result.metadata):
            raise DelegationValidationError("external result contains forbidden mutation intent")
        declared = self._declared_output_types(definition)
        for item in result.typed_outputs:
            if not isinstance(item, dict) or not item.get("type"):
                raise DelegationValidationError("every typed output must declare a type")
            output_type = str(item["type"])
            if output_type not in declared and not any(
                str(port).split(":", 1)[0] == output_type for port in declared
            ):
                raise DelegationValidationError(f"undeclared output type {output_type!r}")
