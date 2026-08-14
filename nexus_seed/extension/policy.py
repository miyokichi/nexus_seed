"""ExtensionPolicy — how much self-extension may proceed without a person.

Configuration, not runtime code (the same argument as
:class:`~nexus_seed.actions.policy.ActionPolicy`): the Runtime never learns that
HIGH means "ask someone".  The table lives on the analyzing process's definition
metadata, so an organisation's appetite for self-extension is a data change.

The default is **conservative on purpose** (spec §42–§43): every risk level
below CRITICAL goes to human review, and CRITICAL is refused outright.  Phase 5A
is the phase in which the system first describes changes to itself; the level of
autonomy it gets is a Phase 5D question, and starting permissive would mean
answering that question by accident.

Deliberately *not* merged with ``ActionPolicy`` or ``PlanSelectionPolicy``.  All
three answer three-way questions, but "how dangerous is this act", "how sure are
we about this reading" and "how much of ourselves would this change" are not the
same question, and one generic policy framework would obscure that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import ExtensionDecision, ExtensionRisk, ExtensionStrategy

#: Conservative default (spec §42): nothing self-extending proceeds unattended.
DEFAULT_RISK_DECISIONS: dict[ExtensionRisk, ExtensionDecision] = {
    ExtensionRisk.LOW: ExtensionDecision.REVIEW,
    ExtensionRisk.MEDIUM: ExtensionDecision.REVIEW,
    ExtensionRisk.HIGH: ExtensionDecision.REVIEW,
    ExtensionRisk.CRITICAL: ExtensionDecision.REJECT,
}

#: Strategies a proposal may take.  ``UNSUPPORTED`` is not one of them: it is
#: the analyzer's way of saying no route exists, not a route.
DEFAULT_ALLOWED_STRATEGIES: tuple[ExtensionStrategy, ...] = tuple(
    s for s in ExtensionStrategy if s is not ExtensionStrategy.UNSUPPORTED
)


@dataclass
class ExtensionPolicy:
    """A risk → decision table, plus the strategies that may be taken at all.

    Attributes:
        decisions: What to do at each :class:`ExtensionRisk`.  A level missing
            from the table falls back to REVIEW — an unclassified extension is
            never silently approved.
        allowed_strategies: Which strategies a proposal may use.  A deployment
            that never wants new code written can remove ``CODE_EXTENSION``
            here, and the validator refuses it by name rather than a reviewer
            having to notice.
        require_human_approval: Force review regardless of risk (Invariant 91 /
            spec §80).  On by default: in Phase 5A a proposal that could change
            the system is never decided by the system alone.
    """

    decisions: dict[ExtensionRisk, ExtensionDecision] = field(
        default_factory=lambda: dict(DEFAULT_RISK_DECISIONS)
    )
    allowed_strategies: tuple[ExtensionStrategy, ...] = DEFAULT_ALLOWED_STRATEGIES
    require_human_approval: bool = True

    def decide(self, risk: ExtensionRisk, *, valid: bool = True) -> ExtensionDecision:
        """What to do with a proposal of this risk.

        An invalid proposal is refused whatever the table says: validation is
        about whether the proposal means anything, and no risk appetite makes a
        meaningless one acceptable.
        """
        if not valid:
            return ExtensionDecision.REJECT
        decision = self.decisions.get(risk, ExtensionDecision.REVIEW)
        if decision is ExtensionDecision.APPROVE and self.require_human_approval:
            return ExtensionDecision.REVIEW
        return decision

    def allows(self, strategy: ExtensionStrategy | None) -> bool:
        """Whether ``strategy`` may be proposed at all."""
        return strategy is not None and strategy in self.allowed_strategies

    def to_dict(self) -> dict:
        """Serialize for ProcessDefinition metadata."""
        return {
            "decisions": {
                risk.value: decision.value for risk, decision in self.decisions.items()
            },
            "allowed_strategies": [s.value for s in self.allowed_strategies],
            "require_human_approval": self.require_human_approval,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ExtensionPolicy":
        """Rebuild from :meth:`to_dict`; ``None``/garbage gives the defaults."""
        if not isinstance(data, dict) or not data:
            return cls()
        decisions = dict(DEFAULT_RISK_DECISIONS)
        raw_decisions = data.get("decisions")
        if not isinstance(raw_decisions, dict):
            raw_decisions = {}
        for key, value in raw_decisions.items():
            risk = ExtensionRisk.coerce(key)
            if risk is None:
                continue
            try:
                decisions[risk] = ExtensionDecision(str(value).upper())
            except ValueError:
                continue
        raw_strategies = data.get("allowed_strategies")
        if isinstance(raw_strategies, list):
            allowed = tuple(
                s
                for s in (ExtensionStrategy.coerce(v) for v in raw_strategies)
                if s is not None and s is not ExtensionStrategy.UNSUPPORTED
            )
        else:
            allowed = DEFAULT_ALLOWED_STRATEGIES
        return cls(
            decisions=decisions,
            allowed_strategies=allowed or DEFAULT_ALLOWED_STRATEGIES,
            require_human_approval=bool(data.get("require_human_approval", True)),
        )


__all__ = [
    "DEFAULT_ALLOWED_STRATEGIES",
    "DEFAULT_RISK_DECISIONS",
    "ExtensionPolicy",
]
