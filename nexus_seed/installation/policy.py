"""Production-installation policy; valid plans still require human review."""

from __future__ import annotations

from dataclasses import dataclass

from .models import InstallationDecision


@dataclass(frozen=True)
class InstallationPolicy:
    """Decide whether a valid plan is reviewable or categorically rejected.

    Phase 5C deliberately has no automatic APPROVE outcome.  Configuration may
    narrow the allowed strategies, but it cannot bypass human production
    review (Invariant 108 and the Phase 5C stopping condition).
    """

    allowed_strategies: tuple[str, ...] = (
        "ADD_EXTRACTOR",
        "ADD_PROCESS_DEFINITION",
        "REGISTER_EXISTING_PROCESS",
    )

    def decide(self, plan, validation) -> InstallationDecision:
        if not validation.ok or plan.strategy not in self.allowed_strategies:
            return InstallationDecision.REJECT
        return InstallationDecision.REVIEW

    def to_dict(self) -> dict:
        return {
            "allowed_strategies": list(self.allowed_strategies),
            "automatic_approval": False,
        }


__all__ = ["InstallationPolicy"]
