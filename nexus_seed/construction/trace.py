"""Joined audit trail from blocked work through sandbox verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConstructionTrace:
    plan: Any
    work_requirement: Any = None
    capability_gap: Any = None
    extension_proposal: Any = None
    approval_decisions: list = field(default_factory=list)
    workspace: Any = None
    grant: Any = None
    actions: list = field(default_factory=list)
    action_executions: list = field(default_factory=list)
    generated_resources: list = field(default_factory=list)
    resource_versions: list = field(default_factory=list)
    representations: list = field(default_factory=list)
    verification_checks: list = field(default_factory=list)
    result: Any = None
    context_snapshot: Any = None
    llm_invocation: Any = None

    @property
    def production_activated(self) -> bool:
        """Always false in Phase 5B (Invariant 101/102)."""
        return False


def get_construction_trace(
    plan_id,
    *,
    construction_store,
    extension_store,
    work_requirement_store,
    action_proposal_store,
    action_execution_store,
    resource_store,
    context_snapshot_store,
    llm_invocation_store,
) -> ConstructionTrace | None:
    plan = construction_store.get_plan(plan_id)
    if plan is None:
        return None
    proposal = extension_store.get_proposal(plan.extension_proposal_id)
    gap = extension_store.get_gap(plan.capability_gap_id)
    workspace = construction_store.get_workspace_for_plan(plan.id)
    actions = [
        p for p in action_proposal_store.all()
        if str(p.parameters.get("construction_plan_id")) == str(plan.id)
    ]
    executions = []
    for action in actions:
        executions.extend(action_execution_store.for_proposal(action.id))
    resources = [
        r for r in resource_store.all_resources()
        if str(r.metadata.get("construction_plan_id")) == str(plan.id)
    ]
    versions = []
    representations = []
    for resource in resources:
        current_versions = resource_store.get_versions(resource.id)
        versions.extend(current_versions)
        for version in current_versions:
            representations.extend(resource_store.list_representations(version.id))
    return ConstructionTrace(
        plan=plan,
        work_requirement=work_requirement_store.get(plan.work_requirement_id),
        capability_gap=gap,
        extension_proposal=proposal,
        approval_decisions=extension_store.decisions_for_proposal(plan.extension_proposal_id),
        workspace=workspace,
        grant=construction_store.get_grant_for_plan(plan.id),
        actions=actions,
        action_executions=executions,
        generated_resources=resources,
        resource_versions=versions,
        representations=representations,
        verification_checks=construction_store.checks_for_plan(plan.id),
        result=construction_store.result_for_plan(plan.id),
        context_snapshot=context_snapshot_store.get(plan.context_snapshot_id) if plan.context_snapshot_id else None,
        llm_invocation=llm_invocation_store.get(plan.llm_invocation_id) if plan.llm_invocation_id else None,
    )


__all__ = ["ConstructionTrace", "get_construction_trace"]
