"""Plan selectors — deciding which eligible plan to run.

Two of them, and the order matters:

* :class:`DeterministicPlanSelector` always works.  Given the same candidates
  and the same preference it picks the same plan on any machine and after any
  restart, and it needs no model, no network and no key.  **NEXUS SEED must be
  able to run entirely without an LLM** (Invariant 77) — so this one is the
  floor, not the fallback of last resort.
* :class:`LLMPlanSelector` asks a model which of the eligible plans looks best.
  Its answer is a :class:`PlanSelectionProposal` — a suggestion that then goes
  through validation and policy like any other (Invariant 74).  It cannot
  invent a plan, cannot reach one outside the candidate set, and cannot
  override a hard constraint (Invariant 76).

The division is the whole point: generation is deterministic, selection may be
assisted (Invariant 75).  An LLM that went missing would cost this system its
judgement about *which* way is best, never its ability to act.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..backends.base import BackendRequest
from .models import METRICS, DecisionPreference, PlanEvaluation, PlanSelectionProposal

logger = logging.getLogger("nexus_seed.decision")

#: What the model is asked for, and all it is asked for (spec §27).
SELECTION_SCHEMA = {
    "selected_plan_id": "str",
    "confidence": "float",
    "rationale": "str",
}

INSTRUCTION = (
    "Several validated plans could satisfy this work. Every one of them is "
    "already known to be runnable and to meet the hard constraints. Choose the "
    "single plan that best fits the stated preference, and say how confident "
    "you are. Answer only with one of the given plan ids: do not invent a plan, "
    "do not describe a different arrangement, do not modify one."
)


@dataclass
class ScoredPlan:
    """One evaluation with its weighted score and the arithmetic behind it."""

    evaluation: PlanEvaluation
    score: float
    contributions: dict

    @property
    def plan_id(self):
        return self.evaluation.plan_id


class DeterministicPlanSelector:
    """Picks a plan by weighted score, with a total order for the ties.

    The ordering (spec §23):

    1. weighted score, higher first;
    2. fewer nodes — the simplest arrangement that scores the same;
    3. fewer stages;
    4. the planner's own rank, which already accounts for capability priority;
    5. the fingerprint, so the last tie is still broken the same way twice.

    Steps 4 and 5 are what make "deterministic" true rather than nearly true.
    Without them two plans that scored identically would be separated by
    dictionary order, which is stable within a run and not across one — and
    step 4 comes first so that with no preference expressed at all, the plan
    chosen is still the one the planner preferred.
    """

    name = "deterministic"
    version = "1"

    def score(
        self, evaluation: PlanEvaluation, preference: DecisionPreference | None
    ) -> ScoredPlan:
        """Weighted score for one plan; unknown metrics contribute nothing.

        Not zero-as-a-value (spec §12) — zero *weighted contribution*, recorded
        as such, because a metric nobody declared should neither help nor hurt
        a plan's ranking.  It has already had its say in the constraint check,
        where unknown is treated conservatively.
        """
        weights = (preference.weights if preference else None) or {}
        contributions: dict = {}
        total = 0.0
        for metric in METRICS:
            weight = _as_float(weights.get(metric))
            if not weight:
                continue
            value = evaluation.metric(metric)
            if value is None:
                contributions[metric] = None
                continue
            contribution = weight * value
            contributions[metric] = contribution
            total += contribution
        return ScoredPlan(evaluation=evaluation, score=total, contributions=contributions)

    def rank(
        self, evaluations: list[PlanEvaluation], preference: DecisionPreference | None = None
    ) -> list[ScoredPlan]:
        """Every plan, best first."""
        scored = [self.score(e, preference) for e in evaluations]
        scored.sort(key=self._order)
        return scored

    def select(
        self, evaluations: list[PlanEvaluation], preference: DecisionPreference | None = None
    ) -> ScoredPlan | None:
        """The one plan to run, or ``None`` when there is nothing to choose from."""
        ranked = self.rank(evaluations, preference)
        return ranked[0] if ranked else None

    @staticmethod
    def _order(scored: ScoredPlan):
        evaluation = scored.evaluation
        return (
            -scored.score,
            evaluation.node_count,
            evaluation.depth,
            evaluation.planner_rank,
            evaluation.fingerprint or "",
        )


class LLMPlanSelector:
    """Asks a model which eligible plan to run; returns a proposal, not a plan.

    Everything it sends is a deterministic summary (spec §61): plan id, shape,
    and the evaluated figures.  The full plan graphs are not sent — they are
    large, they are already validated, and nothing the model is being asked
    depends on them.
    """

    name = "llm"
    version = "1"

    def __init__(self, backend, *, max_candidates: int = 8) -> None:
        self.backend = backend
        self.max_candidates = max_candidates

    def shortlist(
        self,
        evaluations: list[PlanEvaluation],
        preference: DecisionPreference | None = None,
    ) -> list[PlanEvaluation]:
        """The candidates to show, deterministically pre-ranked and truncated.

        A budget is met by dropping the *worst* candidates, never a random
        subset (spec §63): which plans a model was allowed to see must not
        depend on dictionary order.
        """
        ranked = DeterministicPlanSelector().rank(evaluations, preference)
        return [s.evaluation for s in ranked[: self.max_candidates]]

    def build_request(
        self,
        evaluations: list[PlanEvaluation],
        preference: DecisionPreference | None,
        *,
        context: dict | None = None,
        work_summary: dict | None = None,
    ) -> BackendRequest:
        """The prompt: the need, the preference, and the candidate summaries."""
        return BackendRequest(
            instruction=INSTRUCTION,
            context=context or {},
            output_schema=SELECTION_SCHEMA,
            metadata={
                "work": work_summary or {},
                "preference": preference.to_dict() if preference else {},
                "candidates": [e.summary() for e in evaluations],
            },
        )

    async def propose(
        self,
        evaluations: list[PlanEvaluation],
        preference: DecisionPreference | None,
        *,
        work_requirement_id,
        context: dict | None = None,
        work_summary: dict | None = None,
        created_by_process_id=None,
    ):
        """Ask for a selection.  Returns ``(proposal, backend_result, request)``.

        A proposal of ``None`` means the response could not be read as one at
        all — a schema failure, which the caller retries.  A proposal naming a
        plan that does not exist is *not* that: it is a well-formed answer that
        is wrong, and it is recorded as INVALID rather than retried.
        """
        request = self.build_request(
            evaluations, preference, context=context, work_summary=work_summary
        )
        result = await self.backend.execute(request)
        if not result.success:
            return None, result, request
        proposal = PlanSelectionProposal.from_output(
            result.parsed_output,
            work_requirement_id=work_requirement_id,
            candidate_plan_ids=[e.plan_id for e in evaluations],
            created_by_process_id=created_by_process_id,
        )
        return proposal, result, request


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "INSTRUCTION",
    "SELECTION_SCHEMA",
    "DeterministicPlanSelector",
    "LLMPlanSelector",
    "ScoredPlan",
]
