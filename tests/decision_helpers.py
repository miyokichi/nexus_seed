"""Shared scaffolding for the Phase 4C decision tests (not a test module).

The setup every one of these needs is *a choice*: two or more valid ways to do
the same work, so that "which one" is a real question rather than a formality.
``register_alternatives`` builds exactly that, with declared cost / latency /
risk / quality so a preference has something to act on.
"""

from __future__ import annotations

import uuid

from planning_helpers import (  # noqa: F401 - re-exported for the tests
    PlanStatus,
    WorkStatus,
    make_work,
    offer_work,
    planning_runtime,
    register_step,
    status_of,
    wire,
    work_required,
)

from nexus_seed.backends.llm import FakeLLMBackend, failure_response, proposal_response
from nexus_seed.core.event import Event
from nexus_seed.decision.models import (
    DecisionPreference,
    PlanEvaluation,
    SelectionMethod,
    SelectionProposalStatus,
)
from nexus_seed.decision.selector import LLMPlanSelector
from nexus_seed.processes.decision import PLAN_SELECTION_REVIEWED

#: Two extractors that do the same job differently, plus one analyzer.
#:
#: ``fast`` is cheap, quick and risky; ``careful`` is the opposite.  Any
#: preference that means anything separates them.
FAST = {"cost": 1.0, "latency": 2.0, "risk": 0.8, "quality": 0.6, "reliability": 0.7}
CAREFUL = {"cost": 8.0, "latency": 20.0, "risk": 0.1, "quality": 0.95, "reliability": 0.99}
ANALYZE = {"cost": 2.0, "latency": 5.0, "risk": 0.1, "quality": 0.9, "reliability": 0.95}


def register_alternatives(
    runtime, *, fast=FAST, careful=CAREFUL, analyze=ANALYZE, fast_handler=None
):
    """Two ways to extract, one way to analyze — so a plan is a choice."""
    register_step(
        runtime, "fast_extract", "extract", ("raw",), ("m",),
        decision_metadata=fast, priority=10, handler=fast_handler,
    )
    register_step(
        runtime, "careful_extract", "extract", ("raw",), ("m",),
        decision_metadata=careful, priority=1,
    )
    register_step(
        runtime, "analyze", "analyze", ("m",), ("report",), decision_metadata=analyze
    )


def choice_work(runtime, *, preference=None, max_replans=None, **kwargs):
    """A WorkRequirement two different plans could satisfy."""
    kwargs.setdefault("work_key", "choice")
    kwargs.setdefault("required", ("extract", "analyze"))
    kwargs.setdefault("inputs", ("raw",))
    kwargs.setdefault("outputs", ("report",))
    requirement = make_work(runtime, **kwargs)
    if preference is not None or max_replans is not None:
        requirement.decision_preference = preference
        requirement.max_replans = max_replans
        # Re-save under a fresh id: ``save`` is INSERT OR IGNORE on work_key.
        runtime.db.execute(
            "DELETE FROM work_requirements WHERE id = ?", (str(requirement.id),)
        )
        runtime.work_requirement_store.save(requirement)
    return requirement


def compose_only(runtime, *, drop=("select_process_plan",)):
    """Stop the pipeline before selection, so a test can drive it by hand."""
    for name in drop:
        runtime.registry._handlers.pop(name, None)
        # Routing consults persisted definitions, not the in-memory handler
        # map.  Removing only the handler creates a doomed ProcessInstance when
        # ``plan_candidates_ready`` is delivered.  This test helper pauses the
        # pipeline cleanly by taking both halves out; ``restore_selector``
        # registers the unchanged definition again afterwards.
        runtime.db.execute("DELETE FROM process_definitions WHERE name = ?", (name,))


def restore_selector(runtime):
    """Put the selection process back after :func:`compose_only`."""
    from nexus_seed.processes.decision import SELECT_PROCESS_PLAN, select_process_plan

    runtime.register_process(SELECT_PROCESS_PLAN, select_process_plan)


def candidates_ready(requirement) -> Event:
    """The event that asks the decision layer to choose."""
    return Event(
        "plan_candidates_ready", "test", {"work_requirement_id": str(requirement.id)}
    )


def reviewed(proposal_id, decision: str, *, selected_plan_id=None) -> Event:
    """A human's answer about which plan to run."""
    payload = {"proposal_id": str(proposal_id), "decision": decision}
    if selected_plan_id is not None:
        payload["selected_plan_id"] = str(selected_plan_id)
    return Event(PLAN_SELECTION_REVIEWED, "human", payload)


def install_llm(runtime, response, **kwargs):
    """Give the runtime an LLM selector that always answers ``response``."""
    backend = FakeLLMBackend(default=response)
    runtime.set_llm_plan_selector(LLMPlanSelector(backend, **kwargs))
    return backend


def picks(plan_id, *, confidence: float = 0.95, rationale: str = "looks best"):
    """A scripted model answer naming one plan."""
    return proposal_response(
        {
            "selected_plan_id": str(plan_id),
            "confidence": confidence,
            "rationale": rationale,
        }
    )


def picks_nothing_real(*, confidence: float = 0.99):
    """A confident answer naming a plan that does not exist (spec §83)."""
    return picks(uuid.uuid4(), confidence=confidence, rationale="a plan I imagined")


def plan_named(runtime, *names) -> object | None:
    """The plan whose node keys start with ``names``, whatever its status."""
    wanted = list(names)
    for plan in runtime.get_plans():
        keys = [n.node_key.split(":")[0] for n in runtime.get_plan_nodes(plan.id)]
        if keys[: len(wanted)] == wanted:
            return plan
    return None


def statuses(runtime) -> dict:
    """``{first node definition: plan status}`` — a readable plan summary."""
    result = {}
    for plan in runtime.get_plans():
        nodes = runtime.get_plan_nodes(plan.id)
        key = nodes[0].definition_name if nodes else "?"
        result.setdefault(key, []).append(plan.status.value)
    return result


def selection_of(runtime, requirement):
    """The most recent decision made about this need."""
    selections = runtime.get_plan_selections(requirement.id)
    return selections[-1] if selections else None


def evaluation_of(runtime, plan) -> PlanEvaluation | None:
    return runtime.get_plan_evaluation(plan.id)


__all__ = [
    "ANALYZE",
    "CAREFUL",
    "FAST",
    "DecisionPreference",
    "Event",
    "FakeLLMBackend",
    "LLMPlanSelector",
    "PLAN_SELECTION_REVIEWED",
    "PlanEvaluation",
    "PlanStatus",
    "SelectionMethod",
    "SelectionProposalStatus",
    "WorkStatus",
    "candidates_ready",
    "choice_work",
    "compose_only",
    "evaluation_of",
    "failure_response",
    "install_llm",
    "make_work",
    "offer_work",
    "picks",
    "picks_nothing_real",
    "plan_named",
    "planning_runtime",
    "proposal_response",
    "register_alternatives",
    "register_step",
    "restore_selector",
    "reviewed",
    "selection_of",
    "status_of",
    "statuses",
    "wire",
    "work_required",
]
