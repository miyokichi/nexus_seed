"""Decision trace — why this plan, and what happened to the ones before it.

The Phase 4B trace could explain one plan.  Phase 4C creates the question it
could not answer: *there were several ways to do this — why that one?*  And,
after a failure, the harder follow-up: *what changed between the first attempt
and the second?*

The whole chain, from a need to the plan currently running (spec §65, §68)::

    WorkRequirement
      -> candidate plans
      -> evaluations
      -> selection proposal (if a model was asked)
      -> policy decision
      -> selection
      -> selected plan
      -> failure, replan attempt, and round again

Assembled by reading, never by recomputing: a trace that re-ran the evaluator
would show what the system *would* decide now, which is precisely not the
question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..planning.models import PlanStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..backends.base import LLMInvocation
    from ..work.work_requirement import WorkRequirement
    from .models import PlanEvaluation, PlanSelection, PlanSelectionProposal, ReplanAttempt


@dataclass
class CandidateTrace:
    """One plan that was on the table, with its figures and its fate."""

    plan: object
    evaluation: "PlanEvaluation | None" = None
    selected: bool = False

    @property
    def plan_id(self):
        return self.plan.id

    @property
    def status(self) -> str:
        return self.plan.status.value

    @property
    def fingerprint(self) -> str | None:
        return getattr(self.plan, "fingerprint", None)

    @property
    def summary(self) -> dict:
        data = self.evaluation.summary() if self.evaluation else {}
        data["status"] = self.status
        data["selected"] = self.selected
        return data


@dataclass
class DecisionTrace:
    """Every decision made about how to satisfy one need."""

    requirement: "WorkRequirement | None" = None
    candidates: list[CandidateTrace] = field(default_factory=list)
    evaluations: list["PlanEvaluation"] = field(default_factory=list)
    proposals: list["PlanSelectionProposal"] = field(default_factory=list)
    llm_invocations: list["LLMInvocation"] = field(default_factory=list)
    selections: list["PlanSelection"] = field(default_factory=list)
    replan_attempts: list["ReplanAttempt"] = field(default_factory=list)
    plans: list = field(default_factory=list)

    # --- what is happening now ---------------------------------------------

    @property
    def active_plan(self):
        """The plan currently being pursued, if any (at most one, spec §129)."""
        for plan in self.plans:
            if plan.status.active:
                return plan
        return None

    @property
    def selected_plan_id(self):
        """What the most recent decision chose."""
        return self.selections[-1].selected_plan_id if self.selections else None

    @property
    def failed_plans(self) -> list:
        """Every plan that failed terminally, oldest first."""
        return [p for p in self.plans if p.status is PlanStatus.FAILED]

    @property
    def superseded_plans(self) -> list:
        """Candidates that were considered and not chosen."""
        return [p for p in self.plans if p.status is PlanStatus.SUPERSEDED]

    # --- how it got here ----------------------------------------------------

    @property
    def selection_methods(self) -> list[str]:
        """How each decision was reached, in order (spec §37)."""
        return [s.selection_method.value for s in self.selections]

    @property
    def replan_count(self) -> int:
        """How many times this need was replanned after a terminal failure."""
        return len(self.replan_attempts)

    @property
    def reasons(self) -> list[str]:
        """Every recorded reason, in the order the decisions were made."""
        return [r for s in self.selections for r in s.decision_reasons]

    def evaluation_for(self, plan_id) -> "PlanEvaluation | None":
        for evaluation in self.evaluations:
            if evaluation.plan_id == plan_id:
                return evaluation
        return None

    def proposal_for(self, selection) -> "PlanSelectionProposal | None":
        """The suggestion a decision rested on, if it rested on one."""
        if selection.selection_proposal_id is None:
            return None
        for proposal in self.proposals:
            if proposal.id == selection.selection_proposal_id:
                return proposal
        return None

    @property
    def story(self) -> list[str]:
        """The whole decision history as readable lines (debugging aid)."""
        lines = []
        for index, selection in enumerate(self.selections):
            chosen = selection.selected_plan_id
            lines.append(
                f"#{index + 1} {selection.selection_method.value}: "
                f"{'no plan' if chosen is None else chosen} "
                f"from {len(selection.considered_plan_ids)} candidate(s)"
            )
            lines.extend(f"    {r}" for r in selection.decision_reasons)
        for attempt in self.replan_attempts:
            lines.append(
                f"replan #{attempt.attempt_number} after {attempt.previous_plan_id}: "
                f"{attempt.failure_reason or 'terminal failure'}"
            )
        return lines


def get_decision_trace(
    work_requirement_id,
    *,
    work_requirement_store,
    plan_store,
    decision_store,
    llm_invocation_store=None,
) -> DecisionTrace | None:
    """Assemble every decision made about one need, from storage."""
    requirement = work_requirement_store.get(work_requirement_id)
    if requirement is None:
        return None

    plans = plan_store.for_work(work_requirement_id)
    evaluations = decision_store.evaluations_for_work(work_requirement_id)
    selections = decision_store.selections_for_work(work_requirement_id)
    by_plan = {e.plan_id: e for e in evaluations}
    chosen = {s.selected_plan_id for s in selections if s.selected_plan_id}

    proposals = decision_store.proposals_for_work(work_requirement_id)
    invocations = []
    if llm_invocation_store is not None:
        invocations = [
            invocation
            for proposal in proposals
            if proposal.llm_invocation_id is not None
            for invocation in [llm_invocation_store.get(proposal.llm_invocation_id)]
            if invocation is not None
        ]

    return DecisionTrace(
        requirement=requirement,
        plans=plans,
        candidates=[
            CandidateTrace(
                plan=plan,
                evaluation=by_plan.get(plan.id),
                selected=plan.id in chosen,
            )
            for plan in plans
        ],
        evaluations=evaluations,
        proposals=proposals,
        llm_invocations=invocations,
        selections=selections,
        replan_attempts=decision_store.replan_attempts_for_work(work_requirement_id),
    )


__all__ = ["CandidateTrace", "DecisionTrace", "get_decision_trace"]
