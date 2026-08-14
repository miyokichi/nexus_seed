"""Deterministic and deliberately non-self-modifying autonomy policy."""

from __future__ import annotations

from dataclasses import dataclass

from ..extension.models import ExtensionRisk, ExtensionStrategy
from .models import AcquisitionStage, AutonomyDecisionKind


FORBIDDEN_PERMISSIONS = {
    "runtime.modify", "permission.modify", "security.modify", "policy.modify",
    "shell.unrestricted", "network.unrestricted", "host.admin",
}


@dataclass(frozen=True)
class PolicyEvaluation:
    decision: AutonomyDecisionKind
    reasons: tuple[str, ...]
    risk: str
    permissions: tuple[str, ...]
    production_impact: bool
    rollback_available: bool


@dataclass(frozen=True)
class AutonomyPolicy:
    """Conservative default policy for crossing 5A and 5C review gates."""

    name: str = "default_conservative"
    version: str = "1"

    def evaluate_extension(self, proposal) -> PolicyEvaluation:
        """Classify a validated ExtensionProposal without executing it."""

        strategy = ExtensionStrategy.coerce(proposal.strategy)
        risk = ExtensionRisk.coerce(proposal.estimated_risk) or ExtensionRisk.CRITICAL
        permissions = tuple(sorted(set(proposal.required_permissions)))
        component_types = {
            str(component.component_type).upper() for component in proposal.proposed_components
        }
        forbidden_reasons = self._forbidden_reasons(permissions, component_types)
        if strategy in {ExtensionStrategy.CODE_EXTENSION, ExtensionStrategy.ADD_EXTERNAL_PLUGIN}:
            forbidden_reasons.append(f"strategy {strategy.value} is outside the Phase 5D boundary")
        if forbidden_reasons:
            return PolicyEvaluation(
                AutonomyDecisionKind.FORBIDDEN, tuple(forbidden_reasons), risk.value,
                permissions, True, False,
            )
        if (
            strategy is ExtensionStrategy.REGISTER_EXISTING_PROCESS
            and risk is ExtensionRisk.LOW
            # capability.enable is the narrow, short-lived InstallationGrant
            # authority used to publish the exact reviewed provider.  It is
            # not a new global permission granted to that ProcessDefinition.
            and set(permissions) <= {"capability.enable"}
        ):
            return PolicyEvaluation(
                AutonomyDecisionKind.AUTO,
                ("existing verified ProcessDefinition; no new permission",),
                risk.value, permissions, False, True,
            )
        return PolicyEvaluation(
            AutonomyDecisionKind.REVIEW_REQUIRED,
            (f"{strategy.value if strategy else 'UNKNOWN'} changes available production behavior",),
            risk.value, permissions, True, True,
        )

    def evaluate_installation(self, plan, proposal) -> PolicyEvaluation:
        """Classify production promotion; exact artifacts are still revalidated."""

        base = self.evaluate_extension(proposal)
        if base.decision is AutonomyDecisionKind.FORBIDDEN:
            return base
        if (
            base.decision is AutonomyDecisionKind.AUTO
            and plan.strategy == ExtensionStrategy.REGISTER_EXISTING_PROCESS.value
            and bool(plan.rollback_spec)
        ):
            return PolicyEvaluation(
                AutonomyDecisionKind.AUTO,
                ("exact existing definition promotion with rollback metadata",),
                base.risk, base.permissions, True, True,
            )
        return PolicyEvaluation(
            AutonomyDecisionKind.REVIEW_REQUIRED,
            ("new artifact or production behavior requires human promotion review",),
            base.risk, base.permissions, True, bool(plan.rollback_spec),
        )

    @staticmethod
    def _forbidden_reasons(permissions: tuple[str, ...], component_types: set[str]) -> list[str]:
        reasons = [f"forbidden permission: {value}" for value in permissions if value in FORBIDDEN_PERMISSIONS]
        forbidden_components = {
            "RUNTIME", "CORE", "PERMISSION_MODEL", "SECURITY_BOUNDARY",
            "AUTONOMY_POLICY", "POLICY",
        }
        reasons.extend(
            f"forbidden component type: {value}"
            for value in sorted(component_types & forbidden_components)
        )
        return reasons

    def to_dict(self) -> dict:
        """Return stable audit metadata; this policy is code/config, not mutable state."""

        return {"name": self.name, "version": self.version}


__all__ = ["AutonomyPolicy", "FORBIDDEN_PERMISSIONS", "PolicyEvaluation"]
