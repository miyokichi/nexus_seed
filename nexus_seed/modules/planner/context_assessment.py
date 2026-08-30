"""Situation Assessment — read the bootstrap context, say what does not add up.

    terms.md + goals.md + situation.md
      + World Projection + live / blocked / waiting Projects + related Knowledge
        -> SituationAssessment
             -> TaskCandidate -> a person decides -> ProjectOrchestrator.submit()

Five things are looked for, and they are deliberately five *observations*
rather than one verdict:

``terminology_issues``   a word used in more than one sense, or never defined
``contradictions``       two things that cannot both be true
``goal_gaps``            a stated Goal the current situation does not meet
``unknowns``             something that has to be known before anything else
``task_candidates``      what somebody could do about the above

Two boundaries this module keeps, because they are the ones easiest to lose:

* **Goals stay prose.** Nothing here turns "BLOCKED Projectを放置しない" into a
  metric, a threshold or a desired-state record.  It is read as written, and
  what comes out is a sentence about what is missing, not a number.
* **Nothing here starts work.** A TaskCandidate is a suggestion with a person's
  name on the next step.  Approval routes it through the same
  :meth:`ProjectOrchestrator.submit` a human message uses — this module never
  creates a Project, adds a Task, or writes to an orchestrator table.

Without a reasoning backend the assessor finds nothing and records nothing.
That is the honest answer: "no contradiction" and "nobody looked" are
different, and only the second one is true.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...backends.base import BackendRequest, ExecutionBackend
from ..knowledge.bootstrap_context import KIND_CONTEXT_DOCUMENT, ContextDocuments
from ..knowledge.ledger import KnowledgeLedger
from ..knowledge.models import KnowledgeRevision
from ..knowledge.projection import WorldStateProjection

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..project_manager.orchestrator import ProjectOrchestrator

logger = logging.getLogger("nexus_seed.knowledge.context_assessment")

#: One reading of the bootstrap context against the current world.
KIND_CONTEXT_ASSESSMENT = "context_assessment"

#: One thing somebody could do about what that reading found.
KIND_TASK_CANDIDATE = "task_candidate"

CANDIDATE_PENDING_REVIEW = "PENDING_REVIEW"
CANDIDATE_APPROVED = "APPROVED"
CANDIDATE_REJECTED = "REJECTED"
CANDIDATE_ROUTED = "ROUTED"

#: Who the assessment thinks should do this.  ``UNKNOWN`` is a real answer and
#: not a failure: "somebody has to decide who" is worth saying out loud.
ASSIGNEE_AGENT = "AGENT"
ASSIGNEE_HUMAN = "HUMAN"
ASSIGNEE_UNKNOWN = "UNKNOWN"
ASSIGNEE_TYPES = (ASSIGNEE_AGENT, ASSIGNEE_HUMAN, ASSIGNEE_UNKNOWN)

#: The finding lists, in the order a reader should meet them.
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
    """Something somebody could do — never something already being done."""

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
    """One reading of the bootstrap context against the current world."""

    terminology_issues: list[Finding] = field(default_factory=list)
    contradictions: list[Finding] = field(default_factory=list)
    goal_gaps: list[Finding] = field(default_factory=list)
    unknowns: list[Finding] = field(default_factory=list)
    task_candidates: list[TaskCandidate] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Whether this reading found nothing at all."""
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
        """Return one named finding list."""
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
        """Read one backend answer, keeping only what is actually usable.

        An item with no description says nothing, so it is dropped rather than
        stored as an empty finding a person would have to click through.
        """
        if not isinstance(payload, dict):
            return cls()
        return cls(
            **{kind: _findings(payload.get(kind)) for kind in FINDING_KINDS},
            task_candidates=_candidates(payload.get("task_candidates")),
        )


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
    """Same shape of deterministic id the rest of the loop uses."""
    digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()[:20]
    return f"K-{namespace}-{digest}"


def candidate_id(description: str) -> str:
    """The stable id of a Task candidate, derived from what it proposes.

    Two readings that suggest the same thing land on the same object rather
    than queueing the same work twice for a person to reject one of.
    """
    return _stable_id("task-candidate", " ".join(description.split()).lower())


class ContextAssessor:
    """Reads the bootstrap context against the world and records what it finds.

    Everything it writes is Knowledge: the assessment, every candidate, and
    later every human decision about one.  Nothing is executed, and nothing
    reaches the orchestrator until :meth:`route_approved` is called with a
    candidate a person approved.
    """

    def __init__(
        self,
        ledger: KnowledgeLedger,
        documents: ContextDocuments,
        orchestrator: "ProjectOrchestrator",
        *,
        backend: ExecutionBackend | None = None,
        related_knowledge_limit: int = 20,
    ) -> None:
        self.ledger = ledger
        self.documents = documents
        self.orchestrator = orchestrator
        self.backend = backend
        self.related_knowledge_limit = max(0, related_knowledge_limit)

    # --- reads --------------------------------------------------------------

    def assessments(self) -> list[KnowledgeRevision]:
        """Every recorded reading, newest first."""
        return sorted(
            self.ledger.by_kind(KIND_CONTEXT_ASSESSMENT),
            key=lambda item: item.recorded_at,
            reverse=True,
        )

    def candidates(self, *, status: str | None = None) -> list[KnowledgeRevision]:
        """Task candidates, newest first, optionally filtered by status."""
        items = self.ledger.by_kind(KIND_TASK_CANDIDATE)
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.recorded_at, reverse=True)

    def pending(self) -> list[KnowledgeRevision]:
        """Task candidates waiting for a person."""
        return self.candidates(status=CANDIDATE_PENDING_REVIEW)

    # --- assessing ----------------------------------------------------------

    def state_key(self) -> str:
        """A key for "the context and the projects as they currently stand".

        Includes each live project's *status*, so a project going BLOCKED is a
        new situation worth re-reading even though nobody edited a file.
        """
        projects = sorted(
            f"{project.id}:{project.status.value}"
            for project in self.orchestrator.projects.live()
        )
        return f"{self.documents.revision_key()}::{'|'.join(projects)}"

    async def assess(self) -> KnowledgeRevision | None:
        """Read the context once against the current world.

        Returns the recorded assessment, or ``None`` when there was nothing to
        do — no backend, no context, or a situation already read.  The
        already-read check runs *before* the backend call, so a quiet tick
        costs nothing rather than costing a model call whose answer is then
        discarded.
        """
        if self.backend is None:
            return None
        context_documents = self.documents.as_context()
        if not context_documents:
            return None
        key = self.state_key()
        assessment_id = _stable_id("context-assessment", key)
        existing = self.ledger.head(assessment_id)
        if existing is not None:
            return None

        request = BackendRequest(
            instruction=ASSESSMENT_INSTRUCTION,
            context={
                "context_documents": context_documents,
                "world_view": WorldStateProjection(self.ledger).view().snapshot(),
                "projects": self._projects(),
                "related_knowledge": self._related_knowledge(),
            },
            output_schema=ASSESSMENT_SCHEMA,
            metadata={"kind": "context_assessment"},
        )
        result = await self.backend.execute(request)
        if not result.success or not isinstance(result.parsed_output, dict):
            logger.warning("context assessment backend failed: %s", result.error)
            return None
        assessment = SituationAssessment.from_payload(result.parsed_output)

        source_ids = [item["knowledge_id"] for item in context_documents.values()]
        recorded = self.ledger.record(
            assessment.to_dict(),
            knowledge_id=assessment_id,
            source_type="context_assessment",
            format="json",
            kind=KIND_CONTEXT_ASSESSMENT,
            derived_from=source_ids,
            metadata={
                "state_key": key,
                "context_ids": source_ids,
                "model": result.model,
                "counts": {
                    kind: len(assessment.findings(kind)) for kind in FINDING_KINDS
                }
                | {"task_candidates": len(assessment.task_candidates)},
            },
        )
        self._record_candidates(assessment, recorded)
        return recorded

    def _record_candidates(
        self, assessment: SituationAssessment, recorded: KnowledgeRevision
    ) -> list[KnowledgeRevision]:
        """File every candidate for review; never decide one."""
        filed = []
        for candidate in assessment.task_candidates:
            knowledge_id = candidate_id(candidate.description)
            if self.ledger.head(knowledge_id) is not None:
                # The same suggestion, already on somebody's list.  Re-filing it
                # would reset a decision a person already made.
                continue
            filed.append(
                self.ledger.record(
                    candidate.description,
                    knowledge_id=knowledge_id,
                    source_type="context_assessment",
                    source_ref=recorded.knowledge_id,
                    kind=KIND_TASK_CANDIDATE,
                    status=CANDIDATE_PENDING_REVIEW,
                    derived_from=[recorded.knowledge_id, *recorded.derived_from],
                    metadata={
                        **candidate.to_dict(),
                        "assessment_id": recorded.knowledge_id,
                    },
                )
            )
        return filed

    def _projects(self) -> dict[str, list[dict[str, Any]]]:
        """Live projects grouped by what a reader would ask about them."""
        grouped: dict[str, list[dict[str, Any]]] = {
            "active": [],
            "blocked": [],
            "waiting": [],
        }
        for project in self.orchestrator.projects.live():
            status = project.status.value
            if status == "BLOCKED":
                group = "blocked"
            elif status.startswith("WAITING"):
                group = "waiting"
            else:
                group = "active"
            grouped[group].append(project.to_routing_dict())
        return grouped

    def _related_knowledge(self) -> list[dict[str, Any]]:
        """Recent Knowledge that is neither the context nor a reading of it."""
        if not self.related_knowledge_limit:
            return []
        internal = {
            KIND_CONTEXT_ASSESSMENT,
            KIND_TASK_CANDIDATE,
            KIND_CONTEXT_DOCUMENT,
        }
        items = [
            item for item in self.ledger.all_heads() if item.kind not in internal
        ]
        items.sort(key=lambda item: item.recorded_at, reverse=True)
        return [
            {
                "knowledge_id": item.knowledge_id,
                "kind": item.kind,
                "status": item.status,
                "content": item.content.value,
                "recorded_at": item.recorded_at.isoformat(),
            }
            for item in items[: self.related_knowledge_limit]
        ]

    # --- human review -------------------------------------------------------

    async def decide(
        self,
        knowledge_id: str,
        decision: str,
        *,
        actor: str = "human",
        note: str = "",
        description: str | None = None,
    ) -> KnowledgeRevision | None:
        """Record one person's answer to one candidate.

        Three answers, matching what the Cockpit offers:

        ``approve``  do it — routed through the orchestrator's ordinary door.
        ``reject``   do not — kept, with the reason, never deleted.
        ``amend``    not like that — the person's wording replaces the
                     machine's, and the original stays in the object's history.

        An amendment is the interesting one: it is the clearest evidence there
        is of how this system's suggestions differ from what a person actually
        wanted, so it is recorded as a revision rather than an edit and stays
        available to a later Principle Extraction.
        """
        candidate = self.ledger.head(knowledge_id)
        if candidate is None or candidate.kind != KIND_TASK_CANDIDATE:
            return None
        normalized = decision.strip().lower()
        if normalized not in {"approve", "reject", "amend"}:
            raise ValueError("decision must be approve, reject, or amend")

        if normalized == "amend":
            revised = _text(description)
            if not revised:
                raise ValueError("an amended task needs a description")
            if candidate.status not in {CANDIDATE_PENDING_REVIEW, CANDIDATE_REJECTED}:
                # Already approved or routed: the Agent has the old wording, so
                # changing it here would silently disagree with what is running.
                return candidate
            self.ledger.revise(
                knowledge_id,
                value=revised,
                status=CANDIDATE_PENDING_REVIEW,
                metadata={
                    **candidate.metadata,
                    "description": revised,
                    "amended_by": actor,
                    "amended_from": candidate.content.value,
                    "amendment_note": note,
                },
                reason=f"task candidate amended by {actor}",
            )
            return self.ledger.head(knowledge_id)

        if candidate.status != CANDIDATE_PENDING_REVIEW:
            return candidate
        status = CANDIDATE_APPROVED if normalized == "approve" else CANDIDATE_REJECTED
        self.ledger.revise(
            knowledge_id,
            status=status,
            metadata={
                **candidate.metadata,
                "review_actor": actor,
                "review_note": note,
            },
            reason=f"task candidate {normalized}d by {actor}",
        )
        if status == CANDIDATE_APPROVED:
            await self.route_approved()
        return self.ledger.head(knowledge_id)

    async def route_approved(self) -> int:
        """Hand every approved candidate to the orchestrator's ordinary door.

        ``submit`` is the same entry point a human message uses, so the router
        decides whether this is a new Project or another Task on one that
        already exists.  Nothing here creates a Project itself.
        """
        routed = 0
        for candidate in reversed(self.candidates(status=CANDIDATE_APPROVED)):
            metadata = dict(candidate.metadata)
            decision, project = await self.orchestrator.submit(
                str(candidate.content.value),
                source="context_assessment",
                request_id=candidate.knowledge_id,
                project_context={
                    "task_candidate_id": candidate.knowledge_id,
                    "assessment_id": metadata.get("assessment_id"),
                    "reason": metadata.get("reason"),
                    "evidence": list(metadata.get("evidence") or []),
                    "confidence": metadata.get("confidence"),
                    "suggested_assignee_type": metadata.get("suggested_assignee_type"),
                },
            )
            self.ledger.revise(
                candidate.knowledge_id,
                status=CANDIDATE_ROUTED,
                metadata={
                    **metadata,
                    "routing_action": decision.action.value,
                    "project_id": project.id if project is not None else None,
                },
                reason="submitted through ProjectOrchestrator",
            )
            routed += 1
        return routed


__all__ = [
    "ASSESSMENT_INSTRUCTION",
    "ASSESSMENT_SCHEMA",
    "ASSIGNEE_AGENT",
    "ASSIGNEE_HUMAN",
    "ASSIGNEE_TYPES",
    "ASSIGNEE_UNKNOWN",
    "CANDIDATE_APPROVED",
    "CANDIDATE_PENDING_REVIEW",
    "CANDIDATE_REJECTED",
    "CANDIDATE_ROUTED",
    "FINDING_KINDS",
    "KIND_CONTEXT_ASSESSMENT",
    "KIND_TASK_CANDIDATE",
    "ContextAssessor",
    "Finding",
    "SituationAssessment",
    "TaskCandidate",
    "candidate_id",
]
