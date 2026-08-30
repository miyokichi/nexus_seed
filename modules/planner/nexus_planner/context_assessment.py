"""Situation assessment over an explicit planning context.

Planner owns the judgement shape and backend prompt. It does not fetch from
Knowledge and it does not route work to Project Manager; NEXUS SEED composes
those modules by building :class:`PlanningContext`, calling the assessor, and
persisting/routing the returned values where appropriate.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from nexus_planner._support.backends.base import BackendRequest, ExecutionBackend

logger = logging.getLogger("nexus_planner.context_assessment")

KIND_CONTEXT_DOCUMENT = "context_document"
KIND_CONTEXT_ASSESSMENT = "context_assessment"
KIND_TASK_CANDIDATE = "task_candidate"

CANDIDATE_PENDING_REVIEW = "PENDING_REVIEW"
CANDIDATE_APPROVED = "APPROVED"
CANDIDATE_REJECTED = "REJECTED"
CANDIDATE_ROUTED = "ROUTED"

ASSIGNEE_AGENT = "AGENT"
ASSIGNEE_HUMAN = "HUMAN"
ASSIGNEE_UNKNOWN = "UNKNOWN"
ASSIGNEE_TYPES = (ASSIGNEE_AGENT, ASSIGNEE_HUMAN, ASSIGNEE_UNKNOWN)

FINDING_KINDS = (
    "terminology_issues",
    "contradictions",
    "goal_gaps",
    "unknowns",
)

ASSESSMENT_INSTRUCTION = (
    "You read a person's own notes about a system they are running: what the "
    "words mean (terms), what a good state looks like (goals), and what is "
    "true right now (situation). Compare those notes with the live world view "
    "and the projects actually running.\n"
    "Report only what the supplied text and state support:\n"
    "- terminology_issues: a word used in two senses, or used but never defined.\n"
    "- contradictions: two statements that cannot both be true.\n"
    "- goal_gaps: a stated goal the current situation does not meet.\n"
    "- unknowns: something that must be established before the rest can be judged.\n"
    "- task_candidates: concrete things a person or an agent could do about the "
    "above. Say which, in suggested_assignee_type; UNKNOWN is a valid answer.\n"
    "Goals are prose and stay prose. Do not convert a goal into a metric, a "
    "threshold or a target number, and do not invent one that was not written.\n"
    "Quote the text or name the project id you are relying on in `evidence`. "
    "Finding nothing is a correct and common answer: return empty lists rather "
    "than filling them. Return JSON only."
)

_FINDING_ITEM = {
    "type": "object",
    "required": ["description"],
    "properties": {
        "description": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}

ASSESSMENT_SCHEMA = {
    "type": "object",
    "required": list(FINDING_KINDS) + ["task_candidates"],
    "properties": {
        **{kind: {"type": "array", "items": _FINDING_ITEM} for kind in FINDING_KINDS},
        "task_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["description", "reason"],
                "properties": {
                    "description": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "suggested_assignee_type": {
                        "type": "string",
                        "enum": list(ASSIGNEE_TYPES),
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the assessment noticed, with what it is standing on."""

    description: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    """Something somebody could do; never something already being done."""

    description: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    suggested_assignee_type: str = ASSIGNEE_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "suggested_assignee_type": self.suggested_assignee_type,
        }


@dataclass(frozen=True, slots=True)
class SituationAssessment:
    """One reading of the supplied planning context."""

    terminology_issues: list[Finding] = field(default_factory=list)
    contradictions: list[Finding] = field(default_factory=list)
    goal_gaps: list[Finding] = field(default_factory=list)
    unknowns: list[Finding] = field(default_factory=list)
    task_candidates: list[TaskCandidate] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.terminology_issues,
                self.contradictions,
                self.goal_gaps,
                self.unknowns,
                self.task_candidates,
            )
        )

    def findings(self, kind: str) -> list[Finding]:
        return list(getattr(self, kind, []))

    def to_dict(self) -> dict[str, Any]:
        return {
            **{
                kind: [item.to_dict() for item in self.findings(kind)]
                for kind in FINDING_KINDS
            },
            "task_candidates": [item.to_dict() for item in self.task_candidates],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "SituationAssessment":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            **{kind: _findings(payload.get(kind)) for kind in FINDING_KINDS},
            task_candidates=_candidates(payload.get("task_candidates")),
        )


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """All information the composition root gives the Planner for assessment."""

    context_documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    world_view: dict[str, Any] = field(default_factory=dict)
    projects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    related_knowledge: list[dict[str, Any]] = field(default_factory=list)
    revision_key: str = ""

    def state_key(self) -> str:
        if self.revision_key:
            return self.revision_key
        digest = hashlib.sha256(repr(self.to_backend_context()).encode("utf-8")).hexdigest()[:20]
        return f"planning-context:{digest}"

    def to_backend_context(self) -> dict[str, Any]:
        return {
            "context_documents": self.context_documents,
            "world_view": self.world_view,
            "projects": self.projects,
            "related_knowledge": self.related_knowledge,
        }


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """Planner output plus deterministic ids for the composition root to persist."""

    assessment_id: str
    state_key: str
    assessment: SituationAssessment

    @property
    def task_candidates(self) -> list[TaskCandidate]:
        return list(self.assessment.task_candidates)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def _evidence(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _findings(raw: Any) -> list[Finding]:
    if not isinstance(raw, list):
        return []
    findings = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        description = _text(item.get("description"))
        if not description:
            continue
        findings.append(
            Finding(
                description=description,
                evidence=_evidence(item.get("evidence")),
                confidence=_confidence(item.get("confidence")),
            )
        )
    return findings


def _candidates(raw: Any) -> list[TaskCandidate]:
    if not isinstance(raw, list):
        return []
    candidates = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        description = _text(item.get("description"))
        if not description:
            continue
        assignee = _text(item.get("suggested_assignee_type")).upper()
        candidates.append(
            TaskCandidate(
                description=description,
                reason=_text(item.get("reason")),
                evidence=_evidence(item.get("evidence")),
                confidence=_confidence(item.get("confidence")),
                suggested_assignee_type=(
                    assignee if assignee in ASSIGNEE_TYPES else ASSIGNEE_UNKNOWN
                ),
            )
        )
    return candidates


def _stable_id(namespace: str, value: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()[:20]
    return f"K-{namespace}-{digest}"


def candidate_id(description: str) -> str:
    """The stable id of a Task candidate, derived from what it proposes."""
    return _stable_id("task-candidate", " ".join(description.split()).lower())


class ContextAssessor:
    """Reads an explicit planning context and returns an assessment."""

    def __init__(self, *, backend: ExecutionBackend | None = None) -> None:
        self.backend = backend

    async def assess(self, context: PlanningContext) -> AssessmentResult | None:
        """Assess supplied context without reading or writing another module."""
        if self.backend is None or not context.context_documents:
            return None
        key = context.state_key()
        request = BackendRequest(
            instruction=ASSESSMENT_INSTRUCTION,
            context=context.to_backend_context(),
            output_schema=ASSESSMENT_SCHEMA,
            metadata={"kind": "context_assessment"},
        )
        result = await self.backend.execute(request)
        if not result.success or not isinstance(result.parsed_output, dict):
            logger.warning("context assessment backend failed: %s", result.error)
            return None
        return AssessmentResult(
            assessment_id=_stable_id("context-assessment", key),
            state_key=key,
            assessment=SituationAssessment.from_payload(result.parsed_output),
        )


__all__ = [
    "ASSESSMENT_INSTRUCTION",
    "ASSESSMENT_SCHEMA",
    "ASSIGNEE_AGENT",
    "ASSIGNEE_HUMAN",
    "ASSIGNEE_TYPES",
    "ASSIGNEE_UNKNOWN",
    "AssessmentResult",
    "CANDIDATE_APPROVED",
    "CANDIDATE_PENDING_REVIEW",
    "CANDIDATE_REJECTED",
    "CANDIDATE_ROUTED",
    "FINDING_KINDS",
    "KIND_CONTEXT_ASSESSMENT",
    "KIND_CONTEXT_DOCUMENT",
    "KIND_TASK_CANDIDATE",
    "ContextAssessor",
    "Finding",
    "PlanningContext",
    "SituationAssessment",
    "TaskCandidate",
    "candidate_id",
]
