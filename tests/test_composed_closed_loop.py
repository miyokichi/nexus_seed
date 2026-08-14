"""AT28 + AT29 (spec §123, §124): a real file in, a composed plan, a real file out.

Everything from Phase 3D onwards, with composition in the middle: an external
file is observed, catalogued, read, believed, found to need work no single
process can do, planned across three processes, and the last of them acts on
the world through the Phase 3C boundary.

Then the same thing again with the runtime destroyed mid-plan.
"""

from __future__ import annotations

from contextlib import contextmanager

from planning_helpers import (
    PlanStatus,
    WorkStatus,
    declare_capability,
    instances_named,
    pursued_plans,
    wire,
)
from resource_helpers import resource_runtime, watched_tree, write_file

from nexus_seed.backends import LocalFileActionBackend
from nexus_seed.capabilities.models import CapabilityRef
from nexus_seed.context.requirements import (
    ContextRequirements,
    ContinuationReq,
    WorkReq,
)
from nexus_seed.core.process import ProcessDefinition, ProcessStatus
from nexus_seed.processes.actions import (
    action_proposed_event,
    bootstrap_actions,
    waiting_for_action,
)
from nexus_seed.processes import work_intelligence
from nexus_seed.processes.semantic import bootstrap_semantic
from nexus_seed.processes.work_intelligence import IMPACT_ANALYSIS, impact_analysis
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.rules import WORK_CAPABILITIES, WORK_IO_TYPES

DOCUMENT = "D1_CD.raw_measurement=1.23\n"


# --- the three composed steps ---------------------------------------------


async def extract_measurement(ctx):
    """Read the raw value the document carried into world state."""
    entity = "D1_CD"
    raw = ctx.services.get_current_state(entity, "raw_measurement")
    return ctx.complete(
        output={"outputs": [{"type": "measurement", "value": raw.value if raw else None}]}
    )


async def analyze_resistance(ctx):
    measurement = ctx.instance.input.get("inputs", {}).get("measurement")
    return ctx.complete(
        output={
            "outputs": [
                {"type": "resistance_analysis", "value": f"analysis of {measurement}"}
            ]
        }
    )


async def report_writer(ctx):
    """The last step: reach the world through the action boundary."""
    if ctx.resume_point == "await_action":
        return ctx.complete(
            output={
                "outcome": ctx.event.type,
                "outputs": [{"type": "analysis_report", "value": "written"}],
            }
        )
    analysis = ctx.instance.input.get("inputs", {}).get("resistance_analysis")
    proposal = ctx.propose_action(
        backend="local_file",
        action_type="write_file",
        target="composed_report.txt",
        parameters={"content": f"report: {analysis}\n"},
        required_permissions=["filesystem.write"],
        declared_side_effects=["filesystem_write"],
        risk_level="LOW",
        rationale="write the composed analysis report",
    )
    return ctx.suspend(
        resume_point="await_action",
        waiting_for=waiting_for_action(proposal),
        saved_process_state={"action_proposal_id": str(proposal.id)},
        emitted_events=[action_proposed_event(ctx, proposal)],
    )


STEPS = (
    ("extract_measurement", extract_measurement, ("raw_measurement_resource",), ("measurement",)),
    ("analyze_resistance", analyze_resistance, ("measurement",), ("resistance_analysis",)),
    ("generate_analysis_report", report_writer, ("resistance_analysis",), ("analysis_report",)),
)


def register_steps(runtime):
    for name, handler, inputs, outputs in STEPS:
        declare_capability(runtime, name, inputs, outputs)
        runtime.register_process(
            ProcessDefinition(
                name=name,
                version="1",
                handler=name,
                metadata={"role": "work", "permissions": ["filesystem.write"]},
                context_requirements=ContextRequirements(
                    include_trigger_event=True,
                    work=WorkReq(current=True),
                    continuation=ContinuationReq(include=True),
                ),
                provides_capabilities=(CapabilityRef(name),),
            ),
            handler,
        )


def full_stack(runtime, watch_root, out_root):
    """Everything from ingress to action, with composition in the middle."""
    adapter = resource_runtime(runtime, watch_root)
    bootstrap_semantic(runtime)
    runtime.register_process(IMPACT_ANALYSIS, impact_analysis)
    wire(runtime)
    bootstrap_actions(runtime)
    register_steps(runtime)
    backend = LocalFileActionBackend(out_root)
    runtime.register_backend("local_file", backend)
    return adapter, backend


@contextmanager
def composed_work_rule():
    """Make a ``raw_measurement`` change require all three competences.

    Patches the name ``work_intelligence`` actually calls — it imported the
    function directly, so rebinding it on ``work.rules`` would have no effect.
    """
    WORK_CAPABILITIES["composed_analysis"] = (
        "extract_measurement",
        "analyze_resistance",
        "generate_analysis_report",
    )
    WORK_IO_TYPES["composed_analysis"] = (
        ("raw_measurement_resource",),
        ("analysis_report",),
    )
    original = work_intelligence.expected_work_types
    work_intelligence.expected_work_types = lambda entity, attribute: (
        ["composed_analysis"] if attribute == "raw_measurement" else original(entity, attribute)
    )
    try:
        yield
    finally:
        work_intelligence.expected_work_types = original
        WORK_CAPABILITIES.pop("composed_analysis", None)
        WORK_IO_TYPES.pop("composed_analysis", None)


async def test_a_document_drives_a_composed_plan_out_to_a_file(tmp_path):
    """AT28."""
    with composed_work_rule():
        root = watched_tree(tmp_path)
        out = tmp_path / "out"
        runtime = Runtime(tmp_path / "loop.db")
        adapter, backend = full_stack(runtime, root, out)

        write_file(root, "reading.txt", DOCUMENT)
        await adapter.poll_and_ingest()

        # Perception reached world state.
        assert runtime.state_store.get("D1_CD", "raw_measurement") == 1.23

        # No single process could do the work, so a plan was composed.
        requirement = runtime.get_work_requirements()[0]
        assert requirement.work_type == "composed_analysis"
        # Every valid candidate is persisted since Phase 4C; the one that
        # was chosen is the one that is not SUPERSEDED.
        plans = pursued_plans(runtime)
        assert len(plans) == 1
        assert plans[0].status is PlanStatus.COMPLETED

        trace = runtime.get_plan_trace(plans[0].id)
        assert trace.order == [
            "extract_measurement:v1",
            "analyze_resistance:v1",
            "generate_analysis_report:v1",
        ]

        # The last step reached the world through the action boundary.
        assert (out / "composed_report.txt").exists()
        assert "analysis of 1.23" in (out / "composed_report.txt").read_text(encoding="utf-8")
        assert len(backend.calls) == 1
        assert runtime.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
        runtime.close()


async def test_the_composed_loop_survives_a_restart_mid_plan(tmp_path):
    """AT29: crash after the first node; finish without repeating it."""
    with composed_work_rule():
        root = watched_tree(tmp_path)
        out = tmp_path / "out"
        db_path = tmp_path / "restart.db"

        runtime = Runtime(db_path)
        adapter, backend = full_stack(runtime, root, out)
        # Compose but do not execute: no plan executor this time round.
        runtime.registry._handlers.pop("execute_process_plan", None)
        write_file(root, "reading.txt", DOCUMENT)
        await adapter.poll_and_ingest()

        plan = pursued_plans(runtime)[0]
        assert plan.status is PlanStatus.VALIDATED
        assert not (out / "composed_report.txt").exists()
        runtime.close()

        # --- rebuilt with everything; the stored plan is picked up ---
        runtime2 = Runtime(db_path)
        adapter2, backend2 = full_stack(runtime2, root, out)
        from nexus_seed.core.event import Event

        await runtime2.submit_event(
            Event("process_plan_created", "recovery", {"plan_id": str(plan.id)})
        )

        assert runtime2.get_plan(plan.id).status is PlanStatus.COMPLETED
        assert (out / "composed_report.txt").exists()
        # One instance per node, one external effect.
        for name, _, _, _ in STEPS:
            assert len(instances_named(runtime2, name)) == 1
        assert len(backend2.calls) == 1
        assert len(list(out.glob("*.txt"))) == 1

        requirement = runtime2.get_work_requirements()[0]
        assert runtime2.get_work_requirement(requirement.id).status is WorkStatus.SATISFIED
        runtime2.close()
