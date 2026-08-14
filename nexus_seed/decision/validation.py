"""SelectionValidator — nothing runs a plan merely because something chose it.

The same boundary as every proposal in this system, applied to the one thing
Phase 4C newly lets a model influence.  What makes this check different from
the earlier ones is *what can go wrong*: the model is choosing from a list, so
the interesting failure is not a malformed answer but a confident, well-formed
answer naming something that is not on the list — or was on it a moment ago and
is not any more (spec §29, §30).

Every check here is symbolic.  None of them reads the model's rationale: an
explanation is audit material, never evidence (spec §143).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..planning.fingerprint import plan_fingerprint
from ..planning.models import PlanStatus
from .models import DecisionPreference, PlanEvaluation
from .policy import check_constraints


@dataclass
class SelectionValidation:
    """Whether a chosen plan may actually be run, and why not if it may not."""

    ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)


class SelectionValidator:
    """Re-checks a chosen plan against the world as it is *now*.

    Composition, evaluation and selection are three separate moments, and the
    system can move between any two of them: a definition disabled, a
    capability withdrawn, another plan already started.  Discovering that
    before spawning is the entire value of checking twice (spec §30).
    """

    def __init__(self, plan_validator=None) -> None:
        self.plan_validator = plan_validator

    def validate(
        self,
        selected_plan_id,
        *,
        candidate_plan_ids,
        plans: dict,
        graphs: dict,
        evaluations: dict,
        definitions: list,
        preference: DecisionPreference | None = None,
    ) -> SelectionValidation:
        """Check one selection against the candidate set and the current world.

        Args:
            selected_plan_id: What was chosen (may be ``None`` or nonsense).
            candidate_plan_ids: The set the choice had to come from.
            plans: ``{plan_id: ProcessPlan}`` as currently stored.
            graphs: ``{plan_id: (nodes, edges)}``.
            evaluations: ``{plan_id: PlanEvaluation}``.
            definitions: Every ProcessDefinition registered right now.
            preference: The hard constraints that still have to hold.
        """
        result = SelectionValidation()

        if selected_plan_id is None:
            result.fail("no plan was selected")
            return result
        if selected_plan_id not in set(candidate_plan_ids):
            # The hallucination case, and the one that must never be a matter
            # of degree: a plan outside the offered set is not a bold choice.
            result.fail(f"plan {selected_plan_id} is not in the candidate set")
            return result

        plan = plans.get(selected_plan_id)
        if plan is None:
            result.fail(f"plan {selected_plan_id} no longer exists")
            return result
        if plan.status.terminal or plan.status is PlanStatus.RUNNING:
            result.fail(f"plan {selected_plan_id} is already {plan.status.value}")
            return result

        nodes, edges = graphs.get(selected_plan_id, ([], []))
        if not nodes:
            result.fail(f"plan {selected_plan_id} has no nodes")
            return result

        self._check_structure_unchanged(plan, nodes, edges, evaluations, result)
        self._check_still_runnable(nodes, edges, definitions, result)
        self._check_constraints(selected_plan_id, evaluations, preference, result)
        return result

    # --- individual checks -------------------------------------------------

    @staticmethod
    def _check_structure_unchanged(plan, nodes, edges, evaluations, result) -> None:
        """The plan chosen must be the plan that was evaluated (spec §29).

        A plan is immutable by Invariant 57, so this should never fire — which
        is exactly why it is worth asserting: if it ever does, the figures the
        decision rested on described something else.
        """
        evaluation = evaluations.get(plan.id)
        expected = getattr(evaluation, "fingerprint", None) or getattr(
            plan, "fingerprint", None
        )
        if not expected:
            return
        actual = plan_fingerprint(nodes, edges)
        if actual != expected:
            result.fail(
                f"plan {plan.id} changed shape since it was evaluated "
                f"({expected} -> {actual})"
            )

    def _check_still_runnable(self, nodes, edges, definitions, result) -> None:
        """Every definition the plan names is still registered and enabled."""
        if self.plan_validator is None:
            return
        validation = self.plan_validator.validate_before_execution(
            nodes, definitions, edges=edges
        )
        if not validation.ok:
            for reason in validation.reasons:
                result.fail(reason)

    @staticmethod
    def _check_constraints(plan_id, evaluations, preference, result) -> None:
        """Hard constraints hold at selection time, not only at filter time.

        Stated separately from the earlier filter on purpose (Invariant 76):
        the filter decides what may be offered, and this decides what may be
        run.  No confidence, rationale or human approval reaches past it.
        """
        evaluation: PlanEvaluation | None = evaluations.get(plan_id)
        if evaluation is None or preference is None:
            return
        check = check_constraints(evaluation, preference)
        for violation in check.violations:
            result.fail(f"hard constraint: {violation}")


__all__ = ["SelectionValidation", "SelectionValidator"]
