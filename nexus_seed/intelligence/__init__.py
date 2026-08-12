"""Intelligence layer: proposals, validation and policy (the LLM boundary).

Domain data/components, not Runtime primitives.  An LLM proposes; validation and
policy decide whether the proposal may enter the world model.
"""

from .policy import InterpretationPolicy
from .proposal import (
    InterpretationProposal,
    ProposalDecision,
    ProposedStateDelta,
)
from .validation import ValidationResult, validate_proposal

__all__ = [
    "InterpretationPolicy",
    "InterpretationProposal",
    "ProposalDecision",
    "ProposedStateDelta",
    "ValidationResult",
    "validate_proposal",
]
