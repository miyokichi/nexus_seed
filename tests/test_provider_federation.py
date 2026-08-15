"""Phase 5E provider federation acceptance tests."""

from __future__ import annotations

from nexus_seed.capabilities.models import (
    CapabilityMatchStatus,
    CapabilityRef,
    CapabilityRequirement,
)
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.providers import (
    DelegationResult,
    DelegationStatus,
    ExecutionProvider,
    FakeExternalAgentAdapter,
    ProviderBinding,
    ProviderHealth,
    ProviderKind,
)
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.runtime.drain import DrainBudget


async def local_handler(ctx):
    return ctx.complete(output={"ran": "local"})


def definition(name="federated", *, external_only=False):
    return ProcessDefinition(
        name=name,
        version="1",
        handler=f"handler:{name}",
        trigger_event_types=(f"run_{name}",),
        metadata={
            "output_types": ["report"],
            "external_provider_only": external_only,
        },
        provides_capabilities=(CapabilityRef(f"do_{name}"),),
    )


def external(
    runtime,
    definition_,
    adapter,
    *,
    name="agent",
    priority=100,
    kind=ProviderKind.EXTERNAL_AGENT,
):
    provider = runtime.register_provider(
        ExecutionProvider(
            name=name,
            version="1",
            kind=kind,
            adapter_name=f"adapter:{name}",
            health=ProviderHealth.HEALTHY,
            priority=priority,
        ),
        adapter,
    )
    runtime.register_provider_binding(
        ProviderBinding(
            process_definition_name=definition_.name,
            process_definition_version=definition_.version,
            provider_id=provider.id,
            priority=priority,
        )
    )
    return provider


async def test_existing_processes_use_automatic_internal_provider(tmp_path):
    runtime = Runtime(tmp_path / "provider.db")
    item = definition("legacy")
    runtime.register_process(item, local_handler)

    await runtime.submit_event(Event("run_legacy", "test", {}))

    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.COMPLETED
    selections = runtime.get_provider_selections(instance.id)
    assert len(selections) == 1
    selected = runtime.provider_store.get_provider(selections[0].provider_id)
    assert selected.kind is ProviderKind.INTERNAL
    runtime.close()


async def test_external_provider_executes_same_process_contract(tmp_path):
    runtime = Runtime(tmp_path / "provider.db")
    item = definition()
    runtime.register_process(item, local_handler)
    adapter = FakeExternalAgentAdapter(
        [
            DelegationResult(
                invocation_id=None,
                status=DelegationStatus.COMPLETED,
                typed_outputs=[{"type": "report", "value": {"ok": True}}],
            )
        ]
    )
    provider = external(runtime, item, adapter)

    await runtime.submit_event(Event("run_federated", "test", {"x": 1}))

    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.COMPLETED
    assert len(adapter.calls) == 1
    assert adapter.calls[0].process_instance_id == instance.id
    invocation = runtime.get_provider_invocations()[0]
    assert invocation.provider_id == provider.id
    assert invocation.status.value == "COMPLETED"
    assert runtime.get_provider_trace(invocation.id).process_instance.id == instance.id
    runtime.close()


async def test_unavailable_before_start_fails_over_to_internal(tmp_path):
    runtime = Runtime(tmp_path / "provider.db")
    item = definition("failover")
    runtime.register_process(item, local_handler)
    provider = external(runtime, item, FakeExternalAgentAdapter(), name="offline")

    await runtime.submit_event(Event("run_failover", "test", {}))

    instance = runtime.process_store.all_instances()[0]
    assert instance.status is ProcessStatus.COMPLETED
    assert runtime.provider_store.get_provider(provider.id).health is ProviderHealth.UNAVAILABLE
    assert len(runtime.get_provider_selections(instance.id)) == 2
    assert runtime.get_provider_invocations()[0].status.value == "FAILED"
    runtime.close()


async def test_async_delegation_resumes_once_after_restart(tmp_path):
    db_path = tmp_path / "provider.db"
    item = definition("async_agent", external_only=True)
    pending_adapter = FakeExternalAgentAdapter(
        [
            DelegationResult(
                invocation_id=None,
                status=DelegationStatus.PENDING,
                external_run_id="remote-42",
            )
        ]
    )
    runtime = Runtime(db_path)
    runtime.register_process(item, local_handler, bind_internal=False)
    provider = external(runtime, item, pending_adapter, name="remote")

    await runtime.submit_event(
        Event("run_async_agent", "test", {}),
        DrainBudget(max_activations=1),
    )
    instance = runtime.process_store.all_instances()[0]
    invocation = runtime.get_provider_invocations()[0]
    assert instance.status is ProcessStatus.SUSPENDED
    assert invocation.status.value == "WAITING_EXTERNAL"
    runtime.close()

    runtime2 = Runtime(db_path)
    runtime2.register_process(item, local_handler, bind_internal=False)
    runtime2.register_provider_adapter(provider.adapter_name, pending_adapter)
    await runtime2.submit_event(
        Event(
            "provider_result",
            "remote",
            {
                "invocation_id": str(invocation.id),
                "status": "COMPLETED",
                "typed_outputs": [{"type": "report", "value": "done"}],
                "external_run_id": "remote-42",
            },
        ),
        DrainBudget(max_activations=1),
    )

    assert runtime2.process_store.get_instance(instance.id).status is ProcessStatus.COMPLETED
    assert len(pending_adapter.calls) == 1
    assert runtime2.get_provider_invocations()[0].status.value == "COMPLETED"
    runtime2.close()


async def test_external_output_cannot_claim_undeclared_type(tmp_path):
    runtime = Runtime(tmp_path / "provider.db")
    item = definition("unsafe", external_only=True)
    runtime.register_process(item, local_handler, bind_internal=False)
    external(
        runtime,
        item,
        FakeExternalAgentAdapter(
            [
                DelegationResult(
                    invocation_id=None,
                    status=DelegationStatus.COMPLETED,
                    typed_outputs=[{"type": "world_state", "value": {"x": 1}}],
                )
            ]
        ),
    )

    await runtime.submit_event(Event("run_unsafe", "test", {}))

    assert runtime.process_store.all_instances()[0].status is ProcessStatus.FAILED
    assert runtime.state_store.all_current() == []
    runtime.close()


async def test_external_event_to_world_work_and_external_provider(tmp_path):
    """The ordinary semantic/work pipeline is unchanged by execution location."""
    from nexus_seed.capabilities.models import Capability
    from nexus_seed.processes.semantic import bootstrap_semantic
    from nexus_seed.processes.work_intelligence import (
        RESISTANCE_CHECK,
        bootstrap_work_intelligence,
    )
    from nexus_seed.work.work_requirement import WorkStatus

    runtime = Runtime(tmp_path / "provider-e2e.db")
    bootstrap_semantic(runtime)
    bootstrap_work_intelligence(runtime)
    runtime.register_capability(
        Capability(
            name="analyze_resistance",
            input_types=["measurement"],
            output_types=["resistance_analysis"],
        )
    )
    adapter = FakeExternalAgentAdapter(
        [
            DelegationResult(
                invocation_id=None,
                status=DelegationStatus.COMPLETED,
                typed_outputs=[
                    {"type": "resistance_analysis", "value": {"resistance": 12.3}}
                ],
            )
        ]
    )
    external(runtime, RESISTANCE_CHECK, adapter, name="metrology_agent")

    await runtime.submit_event(
        Event(
            "process_parameter_changed",
            "external-test",
            {"parameter": "D1_CD", "old": 48, "new": 45, "unit": "nm"},
        )
    )

    work = runtime.get_work_requirements()[0]
    assert runtime.state_store.get("D1_CD", "target") == 45
    assert runtime.get_work_requirement(work.id).status is WorkStatus.SATISFIED
    assert len(adapter.calls) == 1
    assert runtime.get_provider_invocations()[0].status.value == "COMPLETED"
    runtime.close()


def test_capability_exists_but_provider_missing_is_distinct(tmp_path):
    runtime = Runtime(tmp_path / "provider.db")
    item = definition("provider_gap", external_only=True)
    runtime.register_process(item, local_handler, bind_internal=False)

    result = runtime.capability_matcher.match(
        [CapabilityRequirement("do_provider_gap")],
        runtime.process_store.all_definitions(),
    )

    assert result.status is CapabilityMatchStatus.MISSING_PROVIDER
    assert result.missing_capabilities == []
    runtime.close()


async def test_one_plan_mixes_internal_skill_and_agent_providers(tmp_path):
    from planning_helpers import (
        make_work,
        offer_work,
        only_plan,
        planning_runtime,
        register_chain,
        status_of,
    )
    from nexus_seed.planning.models import PlanStatus
    from nexus_seed.work.work_requirement import WorkStatus

    runtime = planning_runtime(tmp_path)
    steps = register_chain(runtime)
    by_name = {step.name: step for step in steps}
    skill_adapter = FakeExternalAgentAdapter(
        [
            DelegationResult(
                invocation_id=None,
                status=DelegationStatus.COMPLETED,
                typed_outputs=[
                    {"type": "resistance_analysis", "value": "skill-analysis"}
                ],
            )
        ]
    )
    agent_adapter = FakeExternalAgentAdapter(
        [
            DelegationResult(
                invocation_id=None,
                status=DelegationStatus.COMPLETED,
                typed_outputs=[{"type": "analysis_report", "value": "agent-report"}],
            )
        ]
    )
    external(
        runtime,
        by_name["analyze_resistance"],
        skill_adapter,
        name="analysis_skill",
        kind=ProviderKind.EXTERNAL_SKILL,
    )
    external(
        runtime,
        by_name["generate_analysis_report"],
        agent_adapter,
        name="report_agent",
        kind=ProviderKind.EXTERNAL_AGENT,
    )
    work = make_work(runtime)

    await offer_work(runtime, work)

    assert only_plan(runtime).status is PlanStatus.COMPLETED
    assert status_of(runtime, work) is WorkStatus.SATISFIED
    kinds = {
        runtime.provider_store.get_provider(inv.provider_id).kind
        for inv in runtime.get_provider_invocations()
    }
    assert kinds == {ProviderKind.EXTERNAL_SKILL, ProviderKind.EXTERNAL_AGENT}
    assert len(skill_adapter.calls) == len(agent_adapter.calls) == 1
    runtime.close()
