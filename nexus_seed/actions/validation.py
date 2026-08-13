"""Action validation — the gate between wanting to act and being allowed to.

Runs in a fixed order (spec §10), because each stage presumes the previous one
passed::

    Schema validation          is this even a well-formed proposal?
      -> Backend capability    can any registered backend do this at all?
        -> Permission          is the proposing process allowed to?
          -> (Risk policy)     see :mod:`.policy` — a separate decision

Every failure is collected with a reason so the refusal is auditable, and any
failure at all means the action is never handed to a backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..backends.action import BackendCapabilities
from .models import ActionProposal, RiskLevel
from .permissions import missing_permissions


@dataclass
class ActionValidationResult:
    """The outcome of validating an action proposal.

    Attributes:
        schema_ok: The proposal is structurally well-formed.
        capability_ok: A registered backend publishes this ``action_type``.
        permission_ok: The proposing process may perform it.
        mandatory_permissions: What the backend requires for this action type,
            independent of what the proposal declared.
        reasons: Human-readable notes on every failure.
    """

    schema_ok: bool = True
    capability_ok: bool = True
    permission_ok: bool = True
    mandatory_permissions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the proposal cleared every stage."""
        return self.schema_ok and self.capability_ok and self.permission_ok


def validate_schema(proposal: ActionProposal) -> ActionValidationResult:
    """Check that a proposal is structurally usable (spec §11)."""
    result = ActionValidationResult()
    if not proposal.backend:
        result.schema_ok = False
        result.reasons.append("empty backend")
    if not proposal.action_type:
        result.schema_ok = False
        result.reasons.append("empty action_type")
    if not isinstance(proposal.parameters, dict):
        result.schema_ok = False
        result.reasons.append("parameters is not a dict")
    if not isinstance(proposal.required_permissions, list):
        result.schema_ok = False
        result.reasons.append("required_permissions is not a list")
    if not isinstance(proposal.declared_side_effects, list):
        result.schema_ok = False
        result.reasons.append("declared_side_effects is not a list")
    if not isinstance(proposal.risk_level, RiskLevel):
        result.schema_ok = False
        result.reasons.append(f"invalid risk_level {proposal.risk_level!r}")
    return result


def validate_action_proposal(
    proposal: ActionProposal,
    *,
    capabilities: BackendCapabilities | None,
    granted_permissions: list[str],
) -> ActionValidationResult:
    """Run the whole validation pipeline over ``proposal``.

    Args:
        proposal: The candidate action.
        capabilities: What the named backend publishes; ``None`` when no such
            backend is registered (or it publishes no capabilities at all).
        granted_permissions: Permissions held by the proposing process's
            ProcessDefinition.
    """
    result = validate_schema(proposal)
    if not result.schema_ok:
        # Later stages read fields the schema stage just declared unusable.
        result.capability_ok = False
        result.permission_ok = False
        return result

    # --- backend capability (spec §12) ---
    if capabilities is None:
        result.capability_ok = False
        result.reasons.append(
            f"backend {proposal.backend!r} is not registered or publishes no capabilities"
        )
        result.permission_ok = False
        return result

    capability = capabilities.get(proposal.action_type)
    if capability is None:
        result.capability_ok = False
        result.reasons.append(
            f"backend {proposal.backend!r} cannot perform {proposal.action_type!r}"
        )
        result.permission_ok = False
        return result

    mandatory = list(capability.required_permissions)
    result.mandatory_permissions = mandatory

    # --- permissions (spec §16, §17) ---
    # 1. The proposal may not under-declare: a proposal claiming it needs
    #    nothing must not reach a backend action that requires something.
    undeclared = missing_permissions(mandatory, proposal.required_permissions)
    if undeclared:
        result.permission_ok = False
        result.reasons.append(
            "proposal under-declares required_permissions for "
            f"{proposal.action_type!r}: missing {undeclared}"
        )

    # 2. The process must actually hold everything involved — both what it
    #    declared and what the backend mandates.
    needed = list(dict.fromkeys(list(proposal.required_permissions) + mandatory))
    ungranted = missing_permissions(needed, granted_permissions)
    if ungranted:
        result.permission_ok = False
        result.reasons.append(
            f"process is not granted {ungranted} (granted: {sorted(granted_permissions)})"
        )

    return result
