"""Action domain models — the *intention* to act, and the record of acting.

Phase 3C mirrors the Phase 3B perception boundary in the outbound direction.
Nothing here is a core primitive (the six stay fixed); these are domain data
under ``actions/``, exactly like ``world/``, ``work/`` and ``intelligence/``.

The chain deliberately keeps four different things apart::

    Process intention                     (a handler decides it wants something)
      != ActionProposal                   (a durable, not-yet-permitted candidate)
        != approved ActionProposal        (validation + permission + risk said yes)
          != external side effect         (an ActionExecution actually ran it)

:class:`ActionProposal` is the *candidate*, :class:`ActionDecisionRecord` is
*why it was allowed or refused* (permission provenance, spec §49) and
:class:`ActionExecution` is the *journal of what was actually attempted* —
including every failed attempt (Invariant 26).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..core.event import utcnow


class RiskLevel(str, Enum):
    """How dangerous an action is.

    A property of the *proposal*, never a rule baked into the Runtime: the
    mapping from risk to a decision lives in :class:`.policy.ActionPolicy`.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @classmethod
    def coerce(cls, value: Any) -> "RiskLevel | None":
        """Return ``value`` as a RiskLevel, or ``None`` if it is not one."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).upper())
        except (ValueError, AttributeError):
            return None


class ActionProposalStatus(str, Enum):
    """Lifecycle of an :class:`ActionProposal`.

    The first four are *authorization* states (may this run?); the last four are
    *outcome* states (what happened when it ran).  They are deliberately kept in
    one lifecycle but never conflated in meaning (spec §6): nothing reaches
    EXECUTING without having passed through APPROVED.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REVIEW = "REVIEW"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActionExecutionStatus(str, Enum):
    """Outcome of one *attempt* to run an approved action.

    SKIPPED means the idempotency guard found the side effect had already
    happened, so the backend was deliberately not called again (spec §34).
    """

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ActionDecision(str, Enum):
    """What validation + policy concluded about a proposal."""

    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


@dataclass
class ActionProposal:
    """A durable, not-yet-executed candidate action.

    Attributes:
        backend: Name of the registered backend that would run it.
        action_type: Which capability of that backend (e.g. ``"write_file"``).
        target: What the action acts on (a path, a resource id, …).
        parameters: Action-specific arguments.
        required_permissions: Permissions the *proposal declares* it needs.
            Never trusted alone — the backend's mandatory permissions for
            ``action_type`` are checked as well (spec §17).
        declared_side_effects: What the proposer says will change outside.
        risk_level: The proposer's risk classification.
        rationale: Why the process wants this.
        status: Current :class:`ActionProposalStatus`.
        created_by_process_id: The process that proposed it.
        source_work_requirement_id / trigger_event_id / context_snapshot_id:
            provenance back to the work, the event and the compiled context the
            decision was made against (spec §45, §47).
        idempotency_key: Logical identity of the *side effect*.  Two attempts
            sharing a key must produce at most one external effect (spec §33).
        root_proposal_id: First proposal in a modify-chain.  Lifecycle events
            carry it so the proposing process can wait on one stable id even if
            a human replaces the proposal during review.
        replaces_proposal_id: The proposal this one supersedes, if any.
    """

    backend: str
    action_type: str
    target: str | None = None
    parameters: dict = field(default_factory=dict)
    required_permissions: list[str] = field(default_factory=list)
    declared_side_effects: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    rationale: str | None = None
    status: ActionProposalStatus = ActionProposalStatus.PENDING
    created_by_process_id: uuid.UUID | None = None
    source_work_requirement_id: uuid.UUID | None = None
    trigger_event_id: uuid.UUID | None = None
    context_snapshot_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    root_proposal_id: uuid.UUID | None = None
    replaces_proposal_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.root_proposal_id is None:
            self.root_proposal_id = self.id
        if self.idempotency_key is None:
            self.idempotency_key = self.default_idempotency_key()

    def default_idempotency_key(self) -> str:
        """Derive a key from proposal id + the action's semantic identity.

        Proposal-scoped on purpose: retrying *this* proposal must not repeat the
        side effect, but a genuinely new proposal for the same target is a new
        intention and is allowed to act again (spec §33).
        """
        return f"{self.id}:{self.backend}:{self.action_type}:{self.target or ''}"

    def to_dict(self) -> dict:
        """Serialize the action body (what the backend would be asked to do)."""
        return {
            "backend": self.backend,
            "action_type": self.action_type,
            "target": self.target,
            "parameters": self.parameters,
            "required_permissions": list(self.required_permissions),
            "declared_side_effects": list(self.declared_side_effects),
            "risk_level": self.risk_level.value,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(
        cls,
        data: Any,
        *,
        created_by_process_id: uuid.UUID | None = None,
        trigger_event_id: uuid.UUID | None = None,
        root_proposal_id: uuid.UUID | None = None,
        replaces_proposal_id: uuid.UUID | None = None,
    ) -> "ActionProposal | None":
        """Build a proposal from untrusted input (``None`` if unusable).

        Used for a human ``modify`` replacement (spec §24).  A structurally
        impossible body returns ``None``; a *structurally possible but invalid*
        one is returned so it can be REJECTED by validation with a reason.
        """
        if not isinstance(data, dict):
            return None
        risk = RiskLevel.coerce(data.get("risk_level", "LOW"))
        return cls(
            backend=str(data.get("backend", "")),
            action_type=str(data.get("action_type", "")),
            target=data.get("target"),
            parameters=data.get("parameters") if isinstance(data.get("parameters"), dict) else {},
            required_permissions=list(data.get("required_permissions") or [])
            if isinstance(data.get("required_permissions"), list)
            else [],
            declared_side_effects=list(data.get("declared_side_effects") or [])
            if isinstance(data.get("declared_side_effects"), list)
            else [],
            # An unknown risk level is preserved as CRITICAL rather than
            # silently downgraded: unparseable risk must never mean "safe".
            risk_level=risk if risk is not None else RiskLevel.CRITICAL,
            rationale=data.get("rationale"),
            created_by_process_id=created_by_process_id,
            trigger_event_id=trigger_event_id,
            root_proposal_id=root_proposal_id,
            replaces_proposal_id=replaces_proposal_id,
        )


@dataclass
class ActionExecution:
    """One attempt to perform an approved action — success or failure.

    Every attempt is journalled, including backend failures, timeouts and
    idempotent skips (Invariant 26 / spec §28), so "what did this system
    actually try to do to the outside world" is answerable from the database
    alone.
    """

    action_proposal_id: uuid.UUID
    process_instance_id: uuid.UUID
    backend: str
    action_type: str
    status: ActionExecutionStatus = ActionExecutionStatus.STARTED
    attempt: int = 1
    idempotency_key: str | None = None
    result: dict | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class ActionDecisionRecord:
    """Why a proposal was approved, sent to review, or refused.

    Carries permission provenance (spec §49): which ProcessDefinition's grants
    were used, what the proposal declared, and what the backend mandates.
    """

    action_proposal_id: uuid.UUID
    decision: ActionDecision
    risk_level: RiskLevel
    decided_by_process_id: uuid.UUID | None = None
    process_definition_name: str | None = None
    process_definition_version: str | None = None
    granted_permissions: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    mandatory_permissions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    policy: dict = field(default_factory=dict)
    reviewed_by_event_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=utcnow)
