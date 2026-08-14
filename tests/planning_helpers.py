"""Shared scaffolding for the Phase 4B / 4B.1 composition tests (not a test module)."""

from __future__ import annotations

from nexus_seed.capabilities.models import Capability, CapabilityRef, CapabilityRequirement
from nexus_seed.context.requirements import ContextRequirements
from nexus_seed.core.event import Event
from nexus_seed.core.process import ProcessDefinition
from nexus_seed.planning.models import PlanNodeStatus, PlanStatus, Port
from nexus_seed.processes.planning import bootstrap_planning
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
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkRequirement, WorkStatus

RECORDED: list = []

#: The demo chain from the spec: raw resource -> measurement -> analysis -> report.
CHAIN = (
    ("extract_measurement", ("raw_measurement_resource",), ("measurement",)),
    ("analyze_resistance", ("measurement",), ("resistance_analysis",)),
    ("generate_analysis_report", ("resistance_analysis",), ("analysis_report",)),
)

#: The Phase 4B.1 branching DAG (spec §27): P1 -> {P2, P3} -> P4.
#:
#: Phase 4B only ever ran a straight line, so parallel spawn and multi-input
#: join were implemented but never exercised.  This shape is the acceptance
#: case that makes them real.
BRANCHING = (
    ("extract_measurement", ("raw_measurement_resource",), ("measurement",)),
    ("analyze_resistance", ("measurement",), ("resistance_analysis",)),
    ("analyze_thermal", ("measurement",), ("thermal_analysis",)),
    (
        "generate_analysis_report",
        ("resistance_analysis", "thermal_analysis"),
        ("analysis_report",),
    ),
)

BRANCHING_CAPABILITIES = tuple(name for name, _, _ in BRANCHING)


def declare_capability(runtime, name, inputs=(), outputs=()):
    """Register a capability with the types that make it connectable."""
    return runtime.register_capability(
        Capability(name=name, input_types=list(inputs), output_types=list(outputs))
    )


async def staged_worker(ctx):
    """A node handler: consume the declared input, emit the declared output.

    Declared outputs are written as ports (``"type"`` or ``"type:key"``), so a
    process that emits two results of the same type says which is which — the
    producing half of what Phase 4B.1 made explicit.
    """
    node_key = ctx.instance.input.get("plan_node_key", ctx.instance.definition_name)
    produced = ctx.instance.input.get("produces") or _outputs_for(ctx.instance.definition_name)
    received = ctx.instance.input.get("inputs", {})
    RECORDED.append(
        {
            "node": node_key,
            "definition": ctx.instance.definition_name,
            "received": dict(received),
        }
    )
    return ctx.complete(output={"outputs": [_emit(ctx, spec) for spec in produced]})


def _emit(ctx, spec) -> dict:
    """One typed output for a declared port."""
    port = Port.parse(spec)
    produced = {
        "type": port.type,
        "value": f"{port.name}-from-{ctx.instance.definition_name}",
    }
    if port.key is not None:
        produced["key"] = port.key
    return produced


async def failing_worker(ctx):
    """A node handler that always fails terminally."""
    RECORDED.append({"node": ctx.instance.input.get("plan_node_key"), "failed": True})
    return ctx.fail("this node cannot do it")


_OUTPUTS: dict[str, tuple[str, ...]] = {}


def _outputs_for(definition_name: str) -> tuple[str, ...]:
    return _OUTPUTS.get(definition_name, ())


def register_step(
    runtime,
    name: str,
    capability: str,
    inputs=(),
    outputs=(),
    *,
    handler=None,
    priority: int | None = None,
    version: str = "1",
    enabled: bool = True,
    decision_metadata: dict | None = None,
    human_approval_required: bool = False,
):
    """Register a capability with types plus a process that provides it."""
    declare_capability(runtime, capability, inputs, outputs)
    metadata: dict = {"role": "work"}
    if priority is not None:
        metadata["capability_priority"] = priority
    if not enabled:
        metadata["enabled"] = False
    if decision_metadata is not None:
        metadata["decision_metadata"] = dict(decision_metadata)
    if human_approval_required:
        metadata["human_approval_required"] = True
    _OUTPUTS[name] = tuple(outputs)
    definition = ProcessDefinition(
        name=name,
        version=version,
        handler=name,
        metadata=metadata,
        context_requirements=ContextRequirements(include_trigger_event=True),
        provides_capabilities=(CapabilityRef(capability),),
    )
    runtime.register_process(definition, handler or staged_worker)
    return definition


def register_chain(runtime, chain=CHAIN, **kwargs):
    """Register the demo P1 -> P2 -> P3 chain."""
    return [
        register_step(runtime, name, name, inputs, outputs, **kwargs)
        for name, inputs, outputs in chain
    ]


def register_branching(runtime, **kwargs):
    """Register the P1 -> {P2, P3} -> P4 diamond."""
    return register_chain(runtime, BRANCHING, **kwargs)


def make_branching_work(runtime, **kwargs):
    """A WorkRequirement that only the diamond can satisfy."""
    kwargs.setdefault("work_key", "branching")
    kwargs.setdefault("required", BRANCHING_CAPABILITIES)
    return make_work(runtime, **kwargs)


def planning_runtime(tmp_path, name="plan.db"):
    """A runtime with the work pipeline and the planning processes registered."""
    RECORDED.clear()
    _OUTPUTS.clear()
    runtime = Runtime(tmp_path / name)
    return wire(runtime)


def wire(runtime):
    """Register the work + planning processes on an existing runtime."""
    runtime.register_process(WORK_MATCHER, work_matcher)
    runtime.register_process(MISSING_WORK_DETECTOR, missing_work_detector)
    runtime.register_process(WORK_SPAWNER, work_spawner)
    runtime.register_process(RECONCILE_BLOCKED_WORK, reconcile_blocked_work)
    bootstrap_planning(runtime)
    return runtime


def make_work(
    runtime,
    *,
    work_key: str = "w1",
    work_type: str = "composed_demo",
    required=("extract_measurement", "analyze_resistance", "generate_analysis_report"),
    inputs=("raw_measurement_resource",),
    outputs=("analysis_report",),
) -> WorkRequirement:
    """Persist a WorkRequirement that no single process can satisfy."""
    requirement = WorkRequirement(
        work_type=work_type,
        work_key=work_key,
        reason="test",
        required_capabilities=[CapabilityRequirement(name=n) for n in required],
        available_input_types=list(inputs),
        required_output_types=list(outputs),
    )
    runtime.work_requirement_store.save(requirement)
    return requirement


def work_required(requirement) -> Event:
    return Event("work_required", "test", {"work_requirement_id": str(requirement.id)})


async def offer_work(runtime, requirement):
    """Run a requirement through matching, composition and execution."""
    return await runtime.submit_event(work_required(requirement))


def status_of(runtime, requirement) -> WorkStatus:
    return runtime.work_requirement_store.get(requirement.id).status


def only_plan(runtime):
    """The one plan being pursued.

    Since Phase 4C every valid candidate is persisted, so "the plan" means the
    one that was chosen: candidates that were considered and passed over are
    kept as SUPERSEDED and are not it (spec §41).
    """
    plans = [p for p in runtime.get_plans() if p.status is not PlanStatus.SUPERSEDED]
    assert len(plans) == 1, f"expected one pursued plan, got {len(plans)}"
    return plans[0]


def candidate_plans(runtime):
    """Every plan composed for the work, chosen or not."""
    return runtime.get_plans()


def pursued_plans(runtime):
    """The plans that were actually chosen — candidates passed over excluded."""
    return [p for p in runtime.get_plans() if p.status is not PlanStatus.SUPERSEDED]


def node_statuses(runtime, plan) -> dict[str, str]:
    return {n.node_key: n.status.value for n in runtime.get_plan_nodes(plan.id)}


def instances_named(runtime, name) -> list:
    return [i for i in runtime.process_store.all_instances() if i.definition_name == name]


def plan_edges(runtime, plan) -> list:
    """The edges of a plan, read back from storage."""
    return runtime.plan_store.edges(plan.id)


def received_by(node_key: str) -> dict:
    """What the process at ``node_key`` was actually handed."""
    for record in RECORDED:
        if record.get("node") == node_key:
            return record.get("received", {})
    return {}


__all__ = [
    "BRANCHING",
    "BRANCHING_CAPABILITIES",
    "CHAIN",
    "PlanNodeStatus",
    "PlanStatus",
    "Port",
    "RECORDED",
    "WorkStatus",
    "candidate_plans",
    "declare_capability",
    "failing_worker",
    "instances_named",
    "make_branching_work",
    "make_work",
    "node_statuses",
    "offer_work",
    "only_plan",
    "pursued_plans",
    "plan_edges",
    "planning_runtime",
    "received_by",
    "register_branching",
    "register_chain",
    "register_step",
    "staged_worker",
    "status_of",
    "wire",
    "work_required",
]
