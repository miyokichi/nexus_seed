"""ActionPolicy — turn a risk level into APPROVE / REVIEW / REJECT.

Configuration, not runtime code (spec §18): the Runtime never learns that HIGH
means "ask a human".  The mapping lives on the validating process's definition
metadata, so changing the organisation's appetite for risk is a data change.

This is intentionally *not* unified with :class:`InterpretationPolicy` (spec
§19).  Both answer three-way questions, but one weighs confidence in a reading
and the other weighs the danger of an act; forcing them into one generic policy
framework would buy nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import ActionDecision, RiskLevel

#: Conservative default: only LOW acts unattended, CRITICAL never auto-runs.
DEFAULT_RISK_DECISIONS: dict[RiskLevel, ActionDecision] = {
    RiskLevel.LOW: ActionDecision.APPROVE,
    RiskLevel.MEDIUM: ActionDecision.APPROVE,
    RiskLevel.HIGH: ActionDecision.REVIEW,
    RiskLevel.CRITICAL: ActionDecision.REJECT,
}


@dataclass
class ActionPolicy:
    """A risk-level → decision table.

    Attributes:
        decisions: What to do at each :class:`RiskLevel`.  A level missing from
            the table falls back to REVIEW — an unclassified action is never
            silently auto-approved.
    """

    decisions: dict[RiskLevel, ActionDecision] = field(
        default_factory=lambda: dict(DEFAULT_RISK_DECISIONS)
    )

    def decide(self, risk_level: RiskLevel) -> ActionDecision:
        """Return the decision this policy makes for ``risk_level``."""
        return self.decisions.get(risk_level, ActionDecision.REVIEW)

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict (for ProcessDefinition metadata)."""
        return {level.value: decision.value for level, decision in self.decisions.items()}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ActionPolicy":
        """Rebuild from :meth:`to_dict` output; ``None``/garbage gives defaults."""
        if not isinstance(data, dict) or not data:
            return cls()
        decisions = dict(DEFAULT_RISK_DECISIONS)
        for key, value in data.items():
            level = RiskLevel.coerce(key)
            if level is None:
                continue
            try:
                decisions[level] = ActionDecision(str(value).upper())
            except ValueError:
                continue
        return cls(decisions=decisions)
