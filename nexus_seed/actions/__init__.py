"""Action / Tool execution boundary — how NEXUS SEED acts on the world.

The outbound mirror of ``intelligence/``::

    World State / Work -> Process -> ActionProposal -> Validation / Permission /
    Risk Policy -> ExecutionBackend -> ActionExecution -> Result Event -> World

Nothing here is a core primitive; these are domain models and components that
Processes use, exactly like ``world/`` and ``work/``.
"""

from .models import (
    ActionDecision,
    ActionDecisionRecord,
    ActionExecution,
    ActionExecutionStatus,
    ActionProposal,
    ActionProposalStatus,
    RiskLevel,
)
from .permissions import (
    KNOWN_PERMISSIONS,
    PERMISSIONS_METADATA_KEY,
    granted_permissions,
    missing_permissions,
)
from .policy import DEFAULT_RISK_DECISIONS, ActionPolicy
from .trace import ActionTrace, get_action_trace
from .validation import ActionValidationResult, validate_action_proposal, validate_schema

__all__ = [
    "ActionDecision",
    "ActionDecisionRecord",
    "ActionExecution",
    "ActionExecutionStatus",
    "ActionPolicy",
    "ActionProposal",
    "ActionProposalStatus",
    "ActionTrace",
    "ActionValidationResult",
    "DEFAULT_RISK_DECISIONS",
    "KNOWN_PERMISSIONS",
    "PERMISSIONS_METADATA_KEY",
    "RiskLevel",
    "get_action_trace",
    "granted_permissions",
    "missing_permissions",
    "validate_action_proposal",
    "validate_schema",
]
