"""Proposal validation — schema, domain and current-state consistency.

Validation is the gate between an AI proposal and the world model.  A schema or
consistency failure overrides confidence (Invariant 17 / spec §17): a very
confident proposal that contradicts current state must not be auto-accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .proposal import InterpretationProposal

#: Look up the current world-state value of an ``(entity, attribute)``.
CurrentValue = Callable[[str, str], Any]


@dataclass
class ValidationResult:
    """The result of validating a proposal.

    Attributes:
        schema_ok: The proposal is structurally valid.
        consistency_ok: Its ``old_value``s agree with current world state.
        reasons: Human-readable notes on any failures.
    """

    schema_ok: bool = True
    consistency_ok: bool = True
    reasons: list[str] = field(default_factory=list)


def validate_proposal(
    proposal: InterpretationProposal, *, current_value: CurrentValue
) -> ValidationResult:
    """Run schema + consistency validation over a proposal."""
    result = ValidationResult()

    # --- schema validation (spec §13) ---
    if not proposal.subject:
        result.schema_ok = False
        result.reasons.append("empty subject")
    if not (0.0 <= proposal.confidence <= 1.0):
        result.schema_ok = False
        result.reasons.append(f"confidence {proposal.confidence} out of [0,1]")
    if not proposal.proposed_state_deltas:
        result.schema_ok = False
        result.reasons.append("no proposed_state_deltas")
    for delta in proposal.proposed_state_deltas:
        if not delta.entity or not delta.attribute:
            result.schema_ok = False
            result.reasons.append("delta with empty entity/attribute")
        if not (0.0 <= delta.confidence <= 1.0):
            result.schema_ok = False
            result.reasons.append("delta confidence out of [0,1]")

    # --- current-state consistency (spec §15) ---
    for delta in proposal.proposed_state_deltas:
        if delta.old_value is None:
            continue
        current = current_value(delta.entity, delta.attribute)
        if current is not None and current != delta.old_value:
            result.consistency_ok = False
            result.reasons.append(
                f"{delta.entity}.{delta.attribute}: proposal old_value="
                f"{delta.old_value!r} but current={current!r}"
            )

    return result
