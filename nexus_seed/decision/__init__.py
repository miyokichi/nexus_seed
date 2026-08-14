"""Plan selection — choosing between several valid ways to do the same work.

    candidates -> PlanEvaluator -> hard constraints -> selector -> PlanSelection

Phase 4B could compose *a* plan.  This layer answers the question that only
arises once there is more than one: which of these, given what this work is
trying to achieve and what it will not risk?

Two rules shape everything here:

* **generation is deterministic; selection may be assisted** (Invariant 75).  A
  model is never asked what to do, only which of these already-validated things
  to do — and its answer is a proposal that goes through validation and policy
  like every other (Invariant 74).
* **the deterministic selector is the floor, not the fallback** (Invariant 77).
  With no LLM at all, this system still decides and still acts.
"""

from .evaluator import PlanEvaluator
from .models import (
    UNKNOWN_RISK,
    ConstraintCheck,
    DecisionPreference,
    PlanEvaluation,
    PlanSelection,
    PlanSelectionProposal,
    ReplanAttempt,
    SelectionMethod,
    SelectionProposalStatus,
)
from .policy import PlanSelectionPolicy, SelectionDecision, check_constraints, eligible
from .selector import DeterministicPlanSelector, LLMPlanSelector, ScoredPlan
from .trace import CandidateTrace, DecisionTrace, get_decision_trace
from .validation import SelectionValidation, SelectionValidator

__all__ = [
    "UNKNOWN_RISK",
    "CandidateTrace",
    "ConstraintCheck",
    "DecisionPreference",
    "DecisionTrace",
    "DeterministicPlanSelector",
    "LLMPlanSelector",
    "PlanEvaluation",
    "PlanEvaluator",
    "PlanSelection",
    "PlanSelectionPolicy",
    "PlanSelectionProposal",
    "ReplanAttempt",
    "ScoredPlan",
    "SelectionDecision",
    "SelectionMethod",
    "SelectionProposalStatus",
    "SelectionValidation",
    "SelectionValidator",
    "check_constraints",
    "eligible",
    "get_decision_trace",
]
