"""Shared scaffolding for the Phase 4A capability tests (not a test module)."""

from __future__ import annotations

from nexus_seed.capabilities.models import CapabilityRef, CapabilityRequirement
from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus

RECORDED: list = []


def capable_definition(
    name: str,
    provides: tuple[str, ...] = (),
    *,
    version: str = "1",
    priority: int | None = None,
    enabled: bool = True,
    trigger: tuple[str, ...] = (),
) -> ProcessDefinition:
    """A ProcessDefinition declaring the competences it can offer."""
    metadata: dict = {"role": "work"}
    if priority is not None:
        metadata["capability_priority"] = priority
    if not enabled:
        metadata["enabled"] = False
    return ProcessDefinition(
        name=name,
        version=version,
        handler=name,
        trigger_event_types=trigger,
        metadata=metadata,
        context_requirements=ContextRequirements(include_trigger_event=True),
        provides_capabilities=tuple(CapabilityRef(c) for c in provides),
    )


async def worker(ctx):
    """A work handler that records that it ran and satisfies its requirement."""
    RECORDED.append(
        {
            "definition": ctx.instance.definition_name,
            "version": ctx.instance.definition_version,
            "work_key": ctx.instance.work_key,
        }
    )
    ctx.satisfy_work()
    return ctx.complete(output={"done": True})


def register_capable(runtime, name, provides=(), **kwargs):
    """Register a capability-declaring process on ``runtime``."""
    definition = capable_definition(name, provides, **kwargs)
    runtime.register_process(definition, worker)
    return definition


def requirements(*names, **kwargs) -> list[CapabilityRequirement]:
    """Build a list of capability requirements from bare names."""
    return [CapabilityRequirement(name=n, **kwargs) for n in names]


def make_work(
    runtime,
    *,
    work_key: str,
    work_type: str = "demo_work",
    required: tuple[str, ...] = (),
    entities: tuple[str, ...] = ("D1_CD",),
) -> WorkRequirement:
    """Persist a WorkRequirement declaring what it needs, and return it."""
    requirement = WorkRequirement(
        work_type=work_type,
        work_key=work_key,
        related_entities=list(entities),
        reason="test",
        required_capabilities=requirements(*required),
    )
    runtime.work_requirement_store.save(requirement)
    return requirement


def work_required(requirement) -> Event:
    """The event that drives a requirement into the matcher."""
    return Event("work_required", "test", {"work_requirement_id": str(requirement.id)})


async def offer_work(runtime, requirement) -> None:
    """Run a requirement through the work pipeline."""
    await runtime.submit_event(work_required(requirement))


def reload(runtime) -> WorkRequirement:
    """Convenience: re-read a requirement from storage."""
    return runtime.work_requirement_store.get


def status_of(runtime, requirement) -> WorkStatus:
    """The current status of a work requirement."""
    return runtime.work_requirement_store.get(requirement.id).status


def instances_named(runtime, name) -> list:
    """Return every instance of the definition ``name``, oldest first."""
    return [i for i in runtime.process_store.all_instances() if i.definition_name == name]


def work_pipeline(runtime):
    """Register the work-intelligence pipeline (matcher, spawner, reconciler)."""
    from nexus_seed.processes.work_intelligence import (
        MISSING_WORK_DETECTOR,
        RECONCILE_BLOCKED_WORK,
        WORK_MATCHER,
        WORK_SPAWNER,
        missing_work_detector,
        reconcile_blocked_work,
        work_matcher,
        work_spawner,
    )

    RECORDED.clear()
    runtime.register_process(WORK_MATCHER, work_matcher)
    runtime.register_process(MISSING_WORK_DETECTOR, missing_work_detector)
    runtime.register_process(WORK_SPAWNER, work_spawner)
    runtime.register_process(RECONCILE_BLOCKED_WORK, reconcile_blocked_work)
    return runtime


def capability_runtime(tmp_path, name="cap.db"):
    """A runtime with the work pipeline registered and nothing else."""
    return work_pipeline(Runtime(tmp_path / name))
