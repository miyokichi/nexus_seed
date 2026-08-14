"""Shared scaffolding for the Phase 5A self-extension tests (not a test module).

What every one of these needs is a system that *cannot do something* — and,
crucially, different reasons for not being able to.  A gap whose provider is
merely switched off and a gap nothing in the architecture can address should
reach visibly different conclusions, so the helpers here make both cheap to set
up.
"""

from __future__ import annotations

from datetime import datetime, timezone

from capability_helpers import (  # noqa: F401 - re-exported for the tests
    capable_definition,
    instances_named,
    offer_work,
    register_capable,
    status_of,
    work_pipeline,
    work_required,
    worker,
)

from nexus_seed.backends.action import (
    ActionCapability,
    BackendCapabilities,
    FakeActionBackend,
)
from nexus_seed.backends.llm import (
    FakeLLMBackend,
    failure_response,
    invalid_response,
    proposal_response,
)
from nexus_seed.capabilities.models import CapabilityRef, CapabilityRequirement
from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.extension.builder import LLMExtensionProposer
from nexus_seed.extension.models import (
    CapabilityGap,
    CapabilityGapStatus,
    ExtensionProposalStatus,
    ExtensionRisk,
    ExtensionStrategy,
)
from nexus_seed.extension.policy import ExtensionPolicy
from nexus_seed.processes.extension import (
    EXTENSION_REVIEWED,
    bootstrap_extension,
)
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus

#: Hints saying "this capability is about reading a format" (spec §26).
POWERPOINT = {"resource_type": "powerpoint", "representation": "structure"}

#: Hints saying "the mechanical part of this is a file write" (spec §27, §72).
REPOSITORY_WRITE = {"backend_actions": ["write_file"]}


def extension_runtime(tmp_path, name: str = "ext.db", *, policy=None, clock=None) -> Runtime:
    """A runtime with the work pipeline and the self-extension pipeline."""
    runtime = work_pipeline(Runtime(tmp_path / name, clock=clock))
    bootstrap_extension(runtime, policy=policy)
    return runtime


def manual_runtime(tmp_path, name: str = "ext.db", *, policy=None):
    """An extension runtime on a manual clock, for driving the retry loop."""
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    return extension_runtime(tmp_path, name, policy=policy, clock=clock), clock


async def exhaust_retries(runtime, clock, rounds: int = 3) -> None:
    """Advance past each backoff so a retrying process reaches its last attempt."""
    for attempt in range(rounds):
        clock.advance(2**attempt + 1)
        await runtime.tick()


def reopen(tmp_path, name: str = "ext.db", *, policy=None) -> Runtime:
    """Rebuild a runtime from the same database — handlers are code, not data."""
    return extension_runtime(tmp_path, name, policy=policy)


def needs(name: str, hints: dict | None = None) -> CapabilityRequirement:
    """A capability requirement, optionally carrying acquisition hints."""
    metadata = {"extension": dict(hints)} if hints else {}
    return CapabilityRequirement(name=name, metadata=metadata)


def gap_work(
    runtime,
    *,
    work_key: str = "gap_work",
    work_type: str = "demo_work",
    required=(),
    entities=("D1_CD",),
) -> WorkRequirement:
    """Persist a WorkRequirement whose capabilities may or may not exist."""
    requirement = WorkRequirement(
        work_type=work_type,
        work_key=work_key,
        related_entities=list(entities),
        reason="test",
        required_capabilities=[
            r if isinstance(r, CapabilityRequirement) else needs(r) for r in required
        ],
    )
    runtime.work_requirement_store.save(requirement)
    return requirement


async def block(runtime, requirement) -> WorkRequirement:
    """Run a requirement through matching so it blocks, and analyse the gap."""
    await runtime.submit_event(work_required(requirement))
    return runtime.get_work_requirement(requirement.id)


def register_disabled_provider(runtime, name: str, capability: str, *, version="1"):
    """A process that provides ``capability`` but is switched off (spec §71).

    The most valuable thing an analyzer can find: the competence is already
    written and declared, and the "extension" is a flag.
    """
    definition = register_capable(runtime, name, (capability,), version=version)
    runtime.set_capability_enabled(capability, "1", False)
    return definition


def register_configurable(runtime, name: str, capability: str):
    """A registered process declaring it *could* provide ``capability``."""
    definition = ProcessDefinition(
        name=name,
        version="1",
        handler=name,
        metadata={"role": "work", "configurable_capabilities": [capability]},
        context_requirements=ContextRequirements(include_trigger_event=True),
    )
    runtime.register_process(definition, worker)
    return definition


def register_file_backend(runtime, name: str = "files"):
    """A registered action backend that can mechanically write files."""
    backend = FakeActionBackend(
        capabilities=BackendCapabilities(
            backend=name,
            actions={
                "write_file": ActionCapability("write_file", ("filesystem.write",)),
                "read_file": ActionCapability("read_file", ("filesystem.read",)),
            },
        )
    )
    runtime.register_backend(name, backend)
    return backend


# --- gaps and proposals -----------------------------------------------------


def only_gap(runtime) -> CapabilityGap:
    """The single CapabilityGap in the runtime (asserting there is one)."""
    gaps = runtime.get_capability_gaps()
    assert len(gaps) == 1, f"expected one gap, got {[g.missing_names for g in gaps]}"
    return gaps[0]


def only_proposal(runtime):
    """The single ExtensionProposal in the runtime (asserting there is one)."""
    proposals = runtime.get_extension_proposals()
    assert len(proposals) == 1, (
        f"expected one proposal, got {[p.declared_strategy for p in proposals]}"
    )
    return proposals[0]


def strategies(runtime) -> list[str]:
    """Every proposal's strategy, oldest first."""
    return [p.declared_strategy for p in runtime.get_extension_proposals()]


def candidates_for(runtime, gap) -> list:
    """Run the analyzer by hand against the runtime's current environment."""
    return runtime.acquisition_analyzer.analyze(
        gap, environment=runtime.acquisition_environment()
    )


def candidate_strategies(runtime, gap) -> list[str]:
    """The analyzer's routes for a gap, best (most reusing) first."""
    return [c.strategy.value for c in candidates_for(runtime, gap)]


# --- review -----------------------------------------------------------------


def reviewed(proposal_id, decision: str, *, replacement: dict | None = None) -> Event:
    """A person's answer about a proposed self-extension."""
    payload = {"proposal_id": str(proposal_id), "decision": decision}
    if replacement is not None:
        payload["replacement"] = replacement
    return Event(EXTENSION_REVIEWED, "human", payload)


# --- the model --------------------------------------------------------------


def install_extension_llm(runtime, response, **kwargs):
    """Give the runtime a proposer that always answers ``response``."""
    backend = FakeLLMBackend(default=response)
    runtime.set_llm_extension_proposer(LLMExtensionProposer(backend, **kwargs))
    return backend


def elaborates(
    strategy: str,
    *,
    capability: str,
    component_type: str = "RESOURCE_EXTRACTOR",
    component: str = "powerpoint_extractor",
    permissions=("repository.read", "process.register"),
    risk: str = "MEDIUM",
    title: str = "Add a PowerPoint extractor",
) -> object:
    """A scripted model answer elaborating one of the offered routes."""
    return proposal_response(
        {
            "strategy": strategy,
            "title": title,
            "description": f"Add {component} so that {capability} becomes possible.",
            "proposed_components": [
                {
                    "component_type": component_type,
                    "name": component,
                    "purpose": f"provide {capability}",
                    "provides_capabilities": [capability],
                    "requires_capabilities": [],
                    "required_permissions": [],
                }
            ],
            "required_permissions": list(permissions),
            "estimated_risk": risk,
            "rationale": "the extractor registry is the smallest place to add this",
        }
    )


__all__ = [
    "EXTENSION_REVIEWED",
    "POWERPOINT",
    "REPOSITORY_WRITE",
    "CapabilityGap",
    "CapabilityGapStatus",
    "CapabilityRef",
    "CapabilityRequirement",
    "Event",
    "ExtensionPolicy",
    "ExtensionProposalStatus",
    "ExtensionRisk",
    "ExtensionStrategy",
    "FakeLLMBackend",
    "LLMExtensionProposer",
    "ManualClock",
    "ProcessDefinition",
    "Runtime",
    "WorkRequirement",
    "WorkStatus",
    "block",
    "bootstrap_extension",
    "candidate_strategies",
    "candidates_for",
    "capable_definition",
    "elaborates",
    "exhaust_retries",
    "extension_runtime",
    "failure_response",
    "manual_runtime",
    "gap_work",
    "install_extension_llm",
    "instances_named",
    "invalid_response",
    "needs",
    "offer_work",
    "only_gap",
    "only_proposal",
    "proposal_response",
    "register_capable",
    "register_configurable",
    "register_disabled_provider",
    "register_file_backend",
    "reopen",
    "reviewed",
    "status_of",
    "strategies",
    "work_pipeline",
    "work_required",
    "worker",
]
