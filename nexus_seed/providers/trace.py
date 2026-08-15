"""Joined provider/delegation provenance."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProviderTrace:
    """The semantic, operational and execution records for one delegation."""

    invocation: object
    provider: object | None
    binding: object | None
    process_definition: object | None = None
    plan_node: object | None = None
    imported_skill: object | None = None
    selections: list[object] = field(default_factory=list)
    process_instance: object | None = None
    work_requirement: object | None = None
    plan: object | None = None
    context_snapshots: list[object] = field(default_factory=list)
    downstream_processes: list[object] = field(default_factory=list)


def get_provider_trace(invocation_id, *, provider_store, process_store,
                       work_store, plan_store, context_store):
    """Join an invocation back to its Process, work, plan and Context."""
    invocation = provider_store.get_invocation(invocation_id)
    if invocation is None:
        return None
    process = process_store.get_instance(invocation.process_instance_id)
    binding = next(
        (b for b in provider_store.all_bindings() if b.id == invocation.provider_binding_id),
        None,
    )
    work = (
        work_store.get(process.work_requirement_id)
        if process is not None and process.work_requirement_id is not None
        else None
    )
    plan = (
        plan_store.get(process.plan_id)
        if process is not None and process.plan_id is not None
        else None
    )
    definition = (
        process_store.get_definition(
            process.definition_name, process.definition_version
        )
        if process is not None
        else None
    )
    node = (
        plan_store.get_node(process.plan_node_id)
        if process is not None and process.plan_node_id is not None
        else None
    )
    downstream = []
    if process is not None and process.plan_id is not None and node is not None:
        successor_ids = {
            edge.to_node_id
            for edge in plan_store.edges(process.plan_id)
            if edge.from_node_id == node.id
        }
        for successor in plan_store.nodes(process.plan_id):
            if successor.id not in successor_ids or successor.process_instance_id is None:
                continue
            instance = process_store.get_instance(successor.process_instance_id)
            if instance is not None:
                downstream.append(instance)
    imported_skill = next(
        (
            skill
            for skill in provider_store.imported_skills()
            if skill.provider_id == invocation.provider_id
        ),
        None,
    )
    return ProviderTrace(
        invocation=invocation,
        provider=provider_store.get_provider(invocation.provider_id),
        binding=binding,
        process_definition=definition,
        plan_node=node,
        imported_skill=imported_skill,
        selections=provider_store.selections_for_instance(invocation.process_instance_id),
        process_instance=process,
        work_requirement=work,
        plan=plan,
        context_snapshots=context_store.for_instance(invocation.process_instance_id),
        downstream_processes=downstream,
    )
