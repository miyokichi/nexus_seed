"""Joined provenance from active capability back to its verified artifact."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InstallationTrace:
    plan: Any
    construction_result: Any = None
    construction_plan: Any = None
    extension_proposal: Any = None
    capability_gap: Any = None
    work_requirement: Any = None
    verified_artifacts: list = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    decisions: list = field(default_factory=list)
    grant: Any = None
    production_actions: list = field(default_factory=list)
    action_executions: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    result: Any = None
    rollback: Any = None
    activation: Any = None
    capability_registrations: list = field(default_factory=list)
    reconciliation_events: list = field(default_factory=list)


def get_installation_trace(
    plan_id,
    *,
    installation_store,
    construction_store,
    extension_store,
    work_requirement_store,
    resource_store,
    action_proposal_store,
    action_execution_store,
    capability_store,
    event_store,
) -> InstallationTrace | None:
    plan = installation_store.get_plan(plan_id)
    if plan is None:
        return None
    construction_result = construction_store.get_result(plan.construction_result_id)
    construction_plan = (
        construction_store.get_plan(construction_result.construction_plan_id)
        if construction_result else None
    )
    actions = [
        value for value in action_proposal_store.all()
        if str(value.parameters.get("installation_plan_id")) == str(plan.id)
    ]
    executions = []
    for action in actions:
        executions.extend(action_execution_store.for_proposal(action.id))
    activation = installation_store.activation_for_plan(plan.id)
    capabilities = []
    if activation:
        capabilities = [
            capability_store.get(name, activation.component_version)
            or capability_store.get(name, "1")
            for name in activation.capabilities
        ]
        capabilities = [value for value in capabilities if value is not None]
    reconciliation = [
        event for event in event_store.all()
        if event.type in {"capability_available", "work_required", "work_satisfied"}
        and (
            event.payload.get("installation_plan_id") == str(plan.id)
            or event.payload.get("work_requirement_id") == str(plan.work_requirement_id)
        )
    ]
    return InstallationTrace(
        plan=plan,
        construction_result=construction_result,
        construction_plan=construction_plan,
        extension_proposal=extension_store.get_proposal(plan.extension_proposal_id),
        capability_gap=extension_store.get_gap(plan.capability_gap_id),
        work_requirement=work_requirement_store.get(plan.work_requirement_id),
        verified_artifacts=[resource_store.get_version(v.resource_version_id) for v in plan.artifact_versions],
        validation=list(plan.validation_reasons),
        decisions=installation_store.decisions_for_plan(plan.id),
        grant=installation_store.grant_for_plan(plan.id),
        production_actions=actions,
        action_executions=executions,
        checks=installation_store.checks_for_plan(plan.id),
        result=installation_store.result_for_plan(plan.id),
        rollback=installation_store.rollback_for_plan(plan.id),
        activation=activation,
        capability_registrations=capabilities,
        reconciliation_events=reconciliation,
    )


__all__ = ["InstallationTrace", "get_installation_trace"]
