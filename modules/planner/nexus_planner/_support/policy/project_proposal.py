"""Deterministic autonomy policy for Knowledge-derived Project proposals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

KIND_PROJECT_PROPOSAL = "project_proposal"

PROPOSAL_AUTO_APPROVED = "AUTO_APPROVED"
PROPOSAL_PENDING_REVIEW = "PENDING_REVIEW"
PROPOSAL_APPROVED = "APPROVED"
PROPOSAL_REJECTED = "REJECTED"
PROPOSAL_FORBIDDEN = "FORBIDDEN"
PROPOSAL_ROUTED = "ROUTED"


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    """Deterministic autonomy decision for one Project proposal."""

    status: str
    reason: str


class ProjectProposalPolicy:
    """Low-risk, high-confidence read-only work is automatic; the rest waits."""

    def __init__(
        self,
        *,
        minimum_confidence: float = 0.5,
        automatic_confidence: float = 0.8,
    ) -> None:
        self.minimum_confidence = minimum_confidence
        self.automatic_confidence = automatic_confidence

    def decide(self, proposal: dict[str, Any]) -> ProposalDecision:
        """Classify a validated proposal without using an LLM."""
        evidence = proposal.get("evidence_ids") or []
        confidence = _confidence(proposal.get("confidence"))
        risk = str(proposal.get("risk") or "high").lower()
        read_only = proposal.get("read_only") is True
        if not evidence:
            return ProposalDecision(PROPOSAL_FORBIDDEN, "proposal has no evidence")
        if confidence < self.minimum_confidence:
            return ProposalDecision(
                PROPOSAL_FORBIDDEN,
                f"confidence {confidence:.2f} is below {self.minimum_confidence:.2f}",
            )
        if risk == "low" and read_only and confidence >= self.automatic_confidence:
            return ProposalDecision(
                PROPOSAL_AUTO_APPROVED,
                "low-risk read-only proposal with sufficient confidence",
            )
        return ProposalDecision(
            PROPOSAL_PENDING_REVIEW,
            "new, non-read-only, uncertain, or elevated-risk work needs review",
        )


def proposal_id(proposal: dict[str, Any]) -> str:
    """Return the stable identity shared by every proposal-producing flow."""
    identity = json.dumps(
        {
            "objective": proposal["objective"],
            "evidence_ids": sorted(proposal["evidence_ids"]),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(
        f"project-proposal:{identity}".encode("utf-8")
    ).hexdigest()[:20]
    return f"K-project-proposal-{digest}"


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
