"""Composition and plan execution, as ordinary Processes.

Two handlers, no new Runtime feature:

    composition_required -> compose_work_plan   -> a durable, validated plan
    process_plan_created -> execute_process_plan -> spawn / join / repeat

The executor is the interesting one.  It does **not** run anything itself: it
works out which positions in the plan are ready, spawns the ordinary processes
for them, and suspends on the ordinary join.  Everything Phase 2A gave the
runtime — atomic transitions, crash recovery, activation idempotency — applies
to a composed plan for free, because a composed plan is not a new kind of
execution (spec §34, §38).

The restart property falls out of the same reuse: a node that completed has its
status and its instance id on disk, so resuming looks at the plan and continues
from the first position that has not finished (Invariant 61).
"""

from __future__ import annotations

import logging
import uuid

from ..capabilities.models import CapabilityMatchStatus
from ..context.requirements import ContextRequirements, ContinuationReq
from ..core.process import (
    ProcessContext,
    ProcessDefinition,
    ProcessResult,
    ProcessStatus,
    SpawnSpec,
)
from ..decision.evaluator import PlanEvaluator
from ..planning.fingerprint import candidate_fingerprint
from ..planning.models import (
    PlanEdge,
    PlanNode,
    PlanNodeStatus,
    PlanStatus,
    ProcessPlan,
    typed_outputs_of,
)
from ..work.work_requirement import WorkStatus

logger = logging.getLogger("nexus_seed.process.planning")

#: Emitted once every valid candidate for a need has been composed and
#: evaluated.  Composition says what *could* be done; the decision layer that
#: listens for this says what *will* be (Invariant 75).
PLAN_CANDIDATES_READY = "plan_candidates_ready"

#: Emitted when a plan fails terminally and the need it served still stands.
REPLAN_REQUIRED = "replan_required"


def _uuid(value) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


COMPOSE_WORK_PLAN = ProcessDefinition(
    name="compose_work_plan",
    version="1",
    handler="compose_work_plan",
    trigger_event_types=("composition_required",),
    metadata={"role": "planner"},
    context_requirements=ContextRequirements(include_trigger_event=True),
)

EXECUTE_PROCESS_PLAN = ProcessDefinition(
    name="execute_process_plan",
    version="1",
    handler="execute_process_plan",
    trigger_event_types=("process_plan_created",),
    metadata={"role": "plan_executor"},
    context_requirements=ContextRequirements(
        include_trigger_event=True, continuation=ContinuationReq(include=True)
    ),
)


# --- composition -----------------------------------------------------------


async def compose_work_plan(ctx: ProcessContext) -> ProcessResult:
    """Compose and persist every valid way to do work no single process can do.

    Phase 4B took the first candidate that validated.  Since Phase 4C the
    choice is a separate decision made by a separate process (Invariant 75), so
    this one's job ends at *here are the options*: every valid candidate is
    persisted as ``PROPOSED`` — composed and checked, but not yet the one — and
    evaluated, and the decision layer is told they are ready.

    A composed candidate is not an active plan (spec §39).  Only selection
    moves one to ``VALIDATED``, which is what keeps "a plan exists" and "a plan
    was chosen" from being the same fact.
    """
    assert ctx.event is not None and ctx.services is not None
    payload = ctx.event.payload
    requirement_id = _uuid(payload.get("work_requirement_id"))
    requirement = ctx.services.get_work_requirement(requirement_id)
    if requirement is None:
        return ctx.fail(f"work requirement {requirement_id} not found")
    # A need already met is not replanned, however the event got here (spec §91).
    if requirement.resolved or requirement.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW}:
        return ctx.complete(
            output={"planned": False, "reason": f"work is {requirement.status.value}"}
        )

    planner = ctx.services.get_composition_planner()
    validator = ctx.services.get_plan_validator()
    if planner is None or validator is None:
        return ctx.fail("composition planner is not available")

    definitions = ctx.services.get_all_definitions()
    requirements = list(requirement.required_capabilities)
    available = list(requirement.available_input_types)
    outputs = list(requirement.required_output_types)

    candidates = planner.plan(
        requirements,
        definitions,
        available_input_types=available,
        required_output_types=outputs,
    )
    planner.score_candidates(candidates, definitions)

    valid = [
        candidate
        for candidate in candidates
        if validator.validate_candidate(
            candidate,
            definitions,
            available_input_types=available,
            required_output_types=outputs,
        ).ok
    ]
    # Replanning asks for the shapes that already failed to be left out
    # (spec §94); an ordinary composition excludes nothing.
    excluded = set(payload.get("exclude_fingerprints") or ())
    if excluded:
        valid = [c for c in valid if candidate_fingerprint(c) not in excluded]

    if not valid:
        return _no_plan(ctx, requirement, candidates, excluded=excluded)

    attempt = int(payload.get("replan_attempt") or 0)
    supersedes = _uuid(payload.get("supersedes_plan_id"))
    evaluator = PlanEvaluator(definitions)

    created = []
    for rank, candidate in enumerate(valid):
        plan, nodes, edges = _materialize(
            ctx, requirement, candidate, candidates,
            replan_attempt=attempt, supersedes_plan_id=supersedes,
        )
        ctx.create_plan(plan, nodes, edges)
        # ``rank`` is this candidate's place in the planner's own order, kept so
        # that a decision with no preference to go on still lands where Phase
        # 4B would have put it.
        ctx.record_plan_evaluation(
            evaluator.evaluate(plan, nodes, edges, planner_rank=rank)
        )
        created.append((plan, nodes))

    ctx.mark_work(requirement.id, WorkStatus.PLANNED)
    ctx.logger.info(
        "composed %d candidate plan(s) for %s", len(created), requirement.work_key
    )
    return ctx.complete(
        output={
            "candidate_count": len(created),
            "plan_ids": [str(p.id) for p, _ in created],
            "orders": [[n.node_key for n in nodes] for _, nodes in created],
        },
        emitted_events=[
            ctx.new_event(
                PLAN_CANDIDATES_READY,
                {
                    "work_requirement_id": str(requirement.id),
                    "plan_ids": [str(p.id) for p, _ in created],
                    "replan_attempt": attempt,
                },
            )
        ],
    )


def _materialize(
    ctx,
    requirement,
    candidate,
    candidates,
    *,
    replan_attempt: int = 0,
    supersedes_plan_id=None,
):
    """Turn one candidate into a persistable plan with real ids."""
    plan = ProcessPlan(
        work_requirement_id=requirement.id,
        # PROPOSED, not VALIDATED: this is an option, not a decision (spec §39).
        status=PlanStatus.PROPOSED,
        required_capabilities=list(requirement.required_capabilities),
        input_types=list(requirement.available_input_types),
        required_output_types=list(requirement.required_output_types),
        created_by_process_id=ctx.instance.id,
        reasons=list(candidate.reasons),
        fingerprint=candidate_fingerprint(candidate),
        replan_attempt=replan_attempt,
        supersedes_plan_id=supersedes_plan_id,
        # Only what the decision rested on, not the whole registry (spec §64).
        planning_snapshot={
            "definitions": [
                {
                    "name": n.definition_name,
                    "version": n.definition_version,
                    "provides": list(n.provided_capabilities),
                    "inputs": list(n.input_types),
                    "outputs": list(n.output_types),
                }
                for n in candidate.nodes
            ],
            "candidates": [c.to_dict() for c in candidates],
        },
    )
    for node in candidate.nodes:
        node.plan_id = plan.id
    for edge in candidate.edges:
        edge.plan_id = plan.id
    return plan, candidate.nodes, candidate.edges


def _no_plan(ctx, requirement, candidates, *, excluded=()) -> ProcessResult:
    """Report that no valid composition exists — without cancelling the need."""
    reasons = [r for c in candidates for r in c.reasons] or [
        "no composition found within the search bounds"
    ]
    if excluded:
        reasons.append(f"{len(excluded)} previously failed plan shape(s) excluded")
    ctx.logger.info("no valid plan for %s: %s", requirement.work_key, reasons)
    # Capability matching already established that the competence exists; it
    # is the current arrangement that failed.  Phase 4C keeps that distinct
    # from a true capability gap (spec §49–§51).
    ctx.mark_work(requirement.id, WorkStatus.BLOCKED_PLAN)
    attempt = int((ctx.event.payload if ctx.event else {}).get("replan_attempt") or 0)
    events = [
        ctx.new_event(
            "process_plan_unavailable",
            {
                "work_requirement_id": str(requirement.id),
                "required_capabilities": [
                    r.name for r in requirement.required_capabilities
                ],
                "reasons": reasons,
            },
        )
    ]
    if attempt:
        events.append(
            ctx.new_event(
                "replan_unavailable",
                {
                    "work_requirement_id": str(requirement.id),
                    "attempt": attempt,
                    "reasons": reasons,
                },
            )
        )
    return ctx.complete(
        output={"planned": False, "reasons": reasons},
        emitted_events=events,
    )


# --- execution -------------------------------------------------------------


async def execute_process_plan(ctx: ProcessContext) -> ProcessResult:
    """Drive a plan one stage at a time, using ordinary spawn and join."""
    assert ctx.services is not None
    plan_id = _uuid(
        ctx.saved_process_state.get("plan_id")
        or (ctx.event.payload.get("plan_id") if ctx.event else None)
    )
    plans = ctx.services.get_plan_store()
    if plans is None or plan_id is None:
        return ctx.fail("plan executor needs a plan id and a plan store")

    plan = plans.get(plan_id)
    if plan is None:
        return ctx.fail(f"plan {plan_id} not found")
    controlled_work = ctx.services.get_work_requirement(plan.work_requirement_id)
    if controlled_work is not None and controlled_work.status in {WorkStatus.PAUSED, WorkStatus.WAITING_REVIEW, WorkStatus.CANCELLED}:
        return ctx.complete(output={"plan_id": str(plan.id), "ran": False, "reason": controlled_work.status.value})
    if plan.status.terminal:
        return ctx.complete(output={"plan_id": str(plan.id), "status": plan.status.value})

    if plan.status is PlanStatus.PROPOSED:
        # A composed candidate that was never chosen.  Running one would make
        # the selection meaningless and could start two plans for one need
        # (spec §39).
        return ctx.complete(
            output={
                "plan_id": str(plan.id),
                "status": plan.status.value,
                "ran": False,
                "reason": "plan was not selected",
            }
        )

    nodes = plans.nodes(plan.id)
    edges = plans.edges(plan.id)

    finished = _absorb_finished_nodes(ctx, plan, nodes)
    if finished is not None:
        return finished

    ready = _ready_nodes(nodes, edges)
    if not ready:
        return _finish(ctx, plan, nodes)

    blocked = _validate_before_running(ctx, plan, ready, edges)
    if blocked is not None:
        return blocked

    return _spawn_stage(ctx, plan, ready, nodes, edges)


def _absorb_finished_nodes(ctx: ProcessContext, plan, nodes: list[PlanNode]):
    """Record the outcome of nodes whose processes have finished.

    Called on every activation, including the first one after a restart — which
    is how a resumed executor learns what happened while it was gone.
    """
    services = ctx.services
    for node in nodes:
        if node.status not in (PlanNodeStatus.RUNNING, PlanNodeStatus.READY):
            continue
        if node.process_instance_id is None:
            continue
        instance = services.get_process_instance(node.process_instance_id)
        if instance is None:
            continue
        if instance.status is ProcessStatus.COMPLETED:
            ctx.update_plan_node(node.id, PlanNodeStatus.COMPLETED)
            node.status = PlanNodeStatus.COMPLETED
        elif instance.status is ProcessStatus.FAILED:
            ctx.update_plan_node(node.id, PlanNodeStatus.FAILED)
            node.status = PlanNodeStatus.FAILED

    failed = [n for n in nodes if n.status is PlanNodeStatus.FAILED]
    if failed:
        return _fail_plan(ctx, plan, nodes, failed)
    return None


def _fail_plan(ctx: ProcessContext, plan, nodes, failed) -> ProcessResult:
    """A terminal node failure fails the plan — and nothing is undone.

    No compensation (spec §55, §98): whatever earlier nodes did to the world
    stays done, and their results stay on record.  Undoing side effects is a
    separate problem with its own correctness questions, and inventing it here
    would be guesswork.

    Since Phase 4C the failure also *asks a question*: a plan failing is not
    the need failing (Invariant 79), so a ``replan_required`` goes out alongside
    it.  Whether another way exists is for the replanner to work out; this
    process only reports that this way did not.
    """
    plan_id = plan.id
    ctx.update_plan(plan_id, PlanStatus.FAILED)
    keys = [n.node_key for n in failed]
    ctx.logger.error("plan %s failed at %s", plan_id, keys)

    # Failure attribution, so a replan can act on what actually went wrong
    # (spec §53) rather than only on the fact that something did.
    attribution = [
        {
            "node_key": n.node_key,
            "plan_node_id": str(n.id),
            "definition": f"{n.definition_name}:v{n.definition_version}",
            "process_instance_id": str(n.process_instance_id)
            if n.process_instance_id
            else None,
        }
        for n in failed
    ]
    failure = {
        "plan_id": str(plan_id),
        "work_requirement_id": str(plan.work_requirement_id),
        "failed_nodes": keys,
        "failed": attribution,
    }
    return ctx.complete(
        output={"plan_id": str(plan_id), "status": "FAILED", "failed_nodes": keys},
        emitted_events=[
            ctx.new_event("process_plan_failed", dict(failure)),
            ctx.new_event(REPLAN_REQUIRED, dict(failure)),
        ],
    )


def _ready_nodes(nodes: list[PlanNode], edges: list[PlanEdge]) -> list[PlanNode]:
    """Return positions whose predecessors have all completed (spec §36)."""
    by_id = {n.id: n for n in nodes}
    predecessors: dict = {n.id: [] for n in nodes}
    for edge in edges:
        if edge.to_node_id in predecessors:
            predecessors[edge.to_node_id].append(edge.from_node_id)

    ready = []
    for node in nodes:
        if node.status is not PlanNodeStatus.PENDING:
            continue
        if all(
            by_id[p].status is PlanNodeStatus.COMPLETED
            for p in predecessors[node.id]
            if p in by_id
        ):
            ready.append(node)
    return ready


def _validate_before_running(ctx: ProcessContext, plan, ready, edges=None):
    """Re-check the definitions a stage is about to use (spec §67).

    Also refuses a legacy plan whose edges cannot be interpreted unambiguously
    — a Phase 4B plan is honoured only where its data flow is beyond doubt.
    """
    validator = ctx.services.get_plan_validator()
    if validator is None:
        return None
    validation = validator.validate_before_execution(
        ready, ctx.services.get_all_definitions(), edges=edges
    )
    if validation.ok:
        return None
    ctx.update_plan(plan.id, PlanStatus.BLOCKED)
    ctx.logger.warning("plan %s blocked before execution: %s", plan.id, validation.reasons)
    return ctx.complete(
        output={"plan_id": str(plan.id), "status": "BLOCKED", "reasons": validation.reasons},
        emitted_events=[
            ctx.new_event(
                "process_plan_failed",
                {"plan_id": str(plan.id), "reasons": validation.reasons},
            )
        ],
    )


def _spawn_stage(
    ctx: ProcessContext,
    plan,
    ready: list[PlanNode],
    nodes: list[PlanNode],
    edges: list[PlanEdge],
) -> ProcessResult:
    """Spawn every ready position and suspend until they all finish.

    Nodes that are ready together really are independent — the plan is a DAG
    and they share no ancestor edge — so they run in parallel through the
    existing join (spec §37).
    """
    specs = [
        SpawnSpec(
            definition_name=node.definition_name,
            definition_version=node.definition_version,
            input={
                "plan_id": str(plan.id),
                "plan_node_id": str(node.id),
                "plan_node_key": node.node_key,
                "work_requirement_id": str(plan.work_requirement_id),
                "inputs": _resolve_inputs(ctx, node, nodes, edges),
            },
            work_requirement_id=plan.work_requirement_id,
            # The runtime links the created instance back to this position, so
            # a restart can tell "already spawned" from "not yet" (spec §51).
            plan_id=plan.id,
            plan_node_id=node.id,
        )
        for node in ready
    ]

    ctx.update_plan(plan.id, PlanStatus.RUNNING)
    ctx.logger.info("plan %s spawning stage: %s", plan.id, [n.node_key for n in ready])
    return ctx.spawn_and_join(
        specs,
        mode="all",
        resume_point="stage_complete",
        saved_process_state={
            "plan_id": str(plan.id),
            "stage": [n.node_key for n in ready],
        },
    )


def _resolve_inputs(
    ctx: ProcessContext, node: PlanNode, nodes: list[PlanNode], edges: list[PlanEdge]
) -> dict:
    """Resolve one node's inputs by following its incoming edges.

    Phase 4B gathered every completed output into a dict keyed by type, so a
    consumer got whichever producer happened to be visited first.  Since 4B.1
    the *edge* says which producer feeds which input (Invariant 66) — nothing
    is decided here that was not decided at planning time.

    Reads the caller's in-memory nodes rather than the store: the statuses this
    activation just absorbed are staged, not yet committed, and a node that
    finished a moment ago is exactly the one whose output is needed now.
    """
    by_id = {n.id: n for n in nodes}
    resolved: dict = {}

    for edge in edges:
        if edge.to_node_id != node.id:
            continue
        producer = by_id.get(edge.from_node_id)
        if producer is None or producer.process_instance_id is None:
            continue
        instance = ctx.services.get_process_instance(producer.process_instance_id)
        if instance is None:
            continue

        wanted = edge.output_port
        for typed in typed_outputs_of(instance.local_state.get("output")):
            if wanted.matches(typed.port):
                resolved[edge.input_port.name] = typed.value
                break

    return resolved


def _finish(ctx: ProcessContext, plan, nodes) -> ProcessResult:
    """Complete the plan, and judge whether it actually satisfied the work."""
    incomplete = [n for n in nodes if n.status is not PlanNodeStatus.COMPLETED]
    if incomplete:
        ctx.update_plan(plan.id, PlanStatus.FAILED)
        return ctx.complete(
            output={
                "plan_id": str(plan.id),
                "status": "FAILED",
                "incomplete": [n.node_key for n in incomplete],
            }
        )

    satisfied, reasons = evaluate_plan_satisfaction(ctx, plan, nodes)
    ctx.update_plan(plan.id, PlanStatus.COMPLETED)

    emitted = [
        ctx.new_event(
            "process_plan_completed",
            {
                "plan_id": str(plan.id),
                "work_requirement_id": str(plan.work_requirement_id),
                "satisfied": satisfied,
                "reasons": reasons,
            },
        )
    ]
    if satisfied:
        ctx.mark_work(plan.work_requirement_id, WorkStatus.SATISFIED)
        emitted.append(
            ctx.new_event(
                "work_satisfied",
                {"work_requirement_id": str(plan.work_requirement_id)},
            )
        )
    else:
        # All the pieces ran, but the job is not done.  Completing every node
        # is not the same thing as meeting the need (spec §60).
        ctx.mark_work(plan.work_requirement_id, WorkStatus.BLOCKED_CAPABILITY)

    ctx.logger.info("plan %s completed (satisfied=%s)", plan.id, satisfied)
    return ctx.complete(
        output={"plan_id": str(plan.id), "status": "COMPLETED", "satisfied": satisfied},
        emitted_events=emitted,
    )


def evaluate_plan_satisfaction(ctx: ProcessContext, plan, nodes) -> tuple[bool, list[str]]:
    """Decide whether a completed plan actually met the requirement (spec §61).

    Deliberately a separate judgement from "every node finished".  A plan whose
    processes all ran but which never produced the required output type has not
    done the work, and saying otherwise would quietly weaken what SATISFIED
    means everywhere else.
    """
    covered = {c for n in nodes for c in n.provided_capabilities}
    missing_capabilities = [
        r.name for r in plan.required_capabilities if r.required and r.name not in covered
    ]

    produced: set[str] = set()
    for node in nodes:
        if node.process_instance_id is None:
            continue
        instance = ctx.services.get_process_instance(node.process_instance_id)
        if instance is None:
            continue
        produced.update(t.type for t in typed_outputs_of(instance.local_state.get("output")))
    missing_outputs = [t for t in plan.required_output_types if t not in produced]

    reasons = []
    if missing_capabilities:
        reasons.append(f"capabilities not covered: {sorted(missing_capabilities)}")
    if missing_outputs:
        reasons.append(f"required outputs not produced: {sorted(missing_outputs)}")
    return (not reasons), reasons


# --- wiring ----------------------------------------------------------------


def bootstrap_planning(runtime) -> None:
    """Register composition, plan execution, and the decision layer.

    The decision processes come along because since Phase 4C composition does
    not choose (Invariant 75): a runtime that could compose candidates but not
    select one would produce plans and never run them.
    """
    runtime.register_process(COMPOSE_WORK_PLAN, compose_work_plan)
    runtime.register_process(EXECUTE_PROCESS_PLAN, execute_process_plan)
    from .decision import bootstrap_decision

    bootstrap_decision(runtime)


__all__ = [
    "COMPOSE_WORK_PLAN",
    "EXECUTE_PROCESS_PLAN",
    "bootstrap_planning",
    "compose_work_plan",
    "evaluate_plan_satisfaction",
    "execute_process_plan",
]
