"""Durable Knowledge -> Project -> Agent -> Knowledge reconciliation.

The Knowledge Runtime sits above the Project Orchestrator, so this is a small
domain coordinator rather than a second Runtime.  It has no private queue: the
append-only Knowledge Ledger, Resource store, A2A journal and orchestrator
instruction ledger are the queue.  Every external identity is mapped to a
deterministic Knowledge id, and every Project submission uses that proposal id
as its request id.  Re-running :meth:`KnowledgeLoop.reconcile` after a crash
therefore converges instead of duplicating work.

The loop deliberately keeps five things apart::

    evidence -> interpretation -> world view -> project proposal -> project

An LLM may propose the fourth item, but a deterministic policy decides whether
it is automatic, needs review, or is forbidden.  Agent reports come back as raw
Knowledge first; only explicit, high-confidence fact observations enter the
World View automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from ..adapters.manual import ManualAdapter
from ..backends.base import BackendRequest, ExecutionBackend
from ..core.event import Event
from ..orchestrator.models import A2AMessage, A2AMessageType
from ..observation_sources import SYSTEM_SNAPSHOT_EVENT, flatten_snapshot
from .consolidation import Consolidator
from .ledger import KnowledgeLedger
from .models import (
    Annotation,
    KIND_CONSOLIDATED_MEMORY,
    KIND_EXPERIENCE,
    KIND_PRINCIPLE,
    RELATION_ABOUT,
    STATUS_SUPPORTED,
    STATUS_VALIDATED,
    KnowledgeRevision,
    Relation,
)
from .principles import PrincipleExtractor
from .projection import WorldStateProjection, annotate_world_fact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..orchestrator.orchestrator import ProjectOrchestrator

logger = logging.getLogger("nexus_seed.knowledge.loop")


KIND_PROJECT_PROPOSAL = "project_proposal"
KIND_SITUATION_ASSESSMENT = "situation_assessment"
KIND_AGENT_REPORT = "agent_report"
KIND_AGENT_OBSERVATION = "agent_observation"
KIND_ARTIFACT = "artifact"
KIND_QUESTION = "question"
KIND_ENTITY_CANDIDATE = "entity_candidate"
KIND_RESOURCE_OBSERVATION = "resource_observation"
KIND_COMPLETION_REVIEW = "completion_review"
KIND_SOURCE_OBSERVATION = "source_observation"

ARTIFACT_PENDING_REVIEW = "PENDING_REVIEW"
ARTIFACT_APPROVED = "APPROVED"
ARTIFACT_REJECTED = "REJECTED"

PROPOSAL_AUTO_APPROVED = "AUTO_APPROVED"
PROPOSAL_PENDING_REVIEW = "PENDING_REVIEW"
PROPOSAL_APPROVED = "APPROVED"
PROPOSAL_REJECTED = "REJECTED"
PROPOSAL_FORBIDDEN = "FORBIDDEN"
PROPOSAL_ROUTED = "ROUTED"

#: Grouping key for Knowledge that names no subject of its own.
UNASSIGNED_SUBJECT = "__unassigned__"

QUESTION_OPEN = "OPEN"
QUESTION_ANSWERED = "ANSWERED"
ENTITY_UNRESOLVED = "UNRESOLVED"
ENTITY_CONFIRMED = "CONFIRMED"
ENTITY_SEPARATE = "SEPARATE"

ASSESSMENT_INSTRUCTION = (
    "You are the situation evaluator for a project orchestrator. Compare new "
    "evidence with the current world view and live projects. Propose only "
    "concrete, useful project goals justified by evidence. Do not duplicate a "
    "live project. It is correct to propose nothing. A read_only proposal may "
    "inspect or analyse information but must not change an external system. "
    "Every proposal must cite one or more supplied knowledge ids. "
    "`principles` are patterns already earned from past cases: use them to "
    "judge what this evidence is likely to lead to. They are predictions, not "
    "truth, and are never evidence on their own — a proposal still has to cite "
    "the evidence it came from. Return JSON only."
)

ASSESSMENT_SCHEMA = {
    "type": "object",
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "objective",
                    "reason",
                    "evidence_ids",
                    "confidence",
                    "risk",
                    "read_only",
                ],
                "properties": {
                    "objective": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "read_only": {"type": "boolean"},
                    "expected_artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        }
    },
}


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    """Deterministic autonomy decision for one project proposal."""

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


@dataclass(slots=True)
class KnowledgeLoopResult:
    """What one bounded reconciliation pass changed."""

    manual_observations: int = 0
    resource_observations: int = 0
    source_observations: int = 0
    agent_reports: int = 0
    consolidations: int = 0
    principles: int = 0
    proposals: int = 0
    projects_routed: int = 0
    knowledge_events: int = 0
    completion_decisions_reconciled: int = 0
    errors: list[str] = field(default_factory=list)


class KnowledgeLoop:
    """Reconcile durable evidence, proposals, Projects and Agent results."""

    def __init__(
        self,
        runtime,
        orchestrator: "ProjectOrchestrator",
        *,
        backend: ExecutionBackend | None = None,
        policy: ProjectProposalPolicy | None = None,
        assessment_batch_size: int = 20,
        consolidation_threshold: int = 5,
        consolidations_per_pass: int = 1,
        principle_threshold: int = 3,
    ) -> None:
        self.runtime = runtime
        self.orchestrator = orchestrator
        self.backend = backend
        self.policy = policy or ProjectProposalPolicy()
        self.assessment_batch_size = max(1, assessment_batch_size)
        #: How many un-consolidated observations about one subject are worth a
        #: consolidation pass, and how many subjects one pass may compress.
        #: Both bound LLM cost per tick — consolidation is never urgent.
        self.consolidation_threshold = max(2, consolidation_threshold)
        self.consolidations_per_pass = max(1, consolidations_per_pass)
        #: How many consolidated memories / experiences justify looking for a
        #: reusable principle across them.
        self.principle_threshold = max(2, principle_threshold)
        self.ledger = KnowledgeLedger(runtime.knowledge_store)
        self.manual = ManualAdapter("knowledge_manual")
        # Completion remains an Agent report until Knowledge and a person have
        # accepted the deliverables.  Standalone orchestrators keep their
        # historical immediate-completion behaviour.
        self.orchestrator.completion_review_enabled = True
        # The router sees the same Knowledge-derived world view the evaluator sees.
        self.orchestrator.context.world_state_provider = self.world_view

    def world_view(self) -> dict[str, Any]:
        """Return the current Knowledge-derived world projection."""
        return WorldStateProjection(self.ledger).view().snapshot()

    async def record_manual(
        self,
        text: str,
        *,
        source_event_key: str,
        metadata: dict[str, Any] | None = None,
    ):
        """Put one human observation through Ingress, then reconcile it."""
        value = (text or "").strip()
        if not value:
            raise ValueError("text must not be empty")
        if not (source_event_key or "").strip():
            raise ValueError("source_event_key must not be empty")
        envelope = self.manual.envelope(
            event_type="knowledge_observed",
            source_event_key=source_event_key.strip(),
            payload={"text": value},
            metadata=dict(metadata or {}),
        )
        ingress_result = await self.runtime.ingress.ingest(envelope)
        await self.reconcile()
        return ingress_result

    async def start_file_observer(
        self, *, adapter_id: str = "knowledge_local_file", poll_interval: float = 60.0
    ) -> None:
        """Start exactly one durable file observer for the authorized folder."""
        event_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"nexus-seed:knowledge-observer:{adapter_id}"
        )
        if self.runtime.event_store.get(event_id) is None:
            await self.runtime.submit_event(
                Event(
                    id=event_id,
                    type="start_watch_files",
                    source="knowledge_runtime",
                    payload={
                        "adapter_id": adapter_id,
                        "poll_interval": poll_interval,
                    },
                )
            )
        else:
            # The observer or its Continuation is already durable.  Draining is
            # enough after a restart; creating a second observer would double-poll.
            await self.runtime.drain()

    async def reconcile(self) -> KnowledgeLoopResult:
        """Run one bounded, idempotent pass of the autonomous loop."""
        result = KnowledgeLoopResult()
        try:
            result.manual_observations = self._ingest_manual_events()
            result.source_observations = self._ingest_source_observations()
            result.resource_observations = self._ingest_resources()
            result.agent_reports = self._ingest_agent_messages()
            result.completion_decisions_reconciled = (
                await self._reconcile_completion_decisions()
            )
            result.knowledge_events = self._ensure_knowledge_events()
            # Re-read before deciding: compress what has piled up, and look for
            # a reusable principle across it, so assessment reasons over
            # digested memory and earned principles rather than raw scraps only.
            result.consolidations = await self._consolidate_knowledge()
            result.principles = await self._extract_principles()
            result.proposals = await self._assess_new_knowledge()
            result.projects_routed = await self._route_approved_proposals()
        except Exception as exc:  # noqa: BLE001 - next tick must remain usable
            logger.exception("knowledge reconciliation failed")
            result.errors.append(str(exc))
        return result

    def proposals(self, *, status: str | None = None) -> list[KnowledgeRevision]:
        """Return current project proposals, newest first."""
        proposals = self.ledger.by_kind(KIND_PROJECT_PROPOSAL)
        if status is not None:
            proposals = [item for item in proposals if item.status == status]
        return sorted(proposals, key=lambda item: item.recorded_at, reverse=True)

    def artifacts(self) -> list[KnowledgeRevision]:
        """Return Agent-produced artifacts, newest first."""
        return sorted(
            self.ledger.by_kind(KIND_ARTIFACT),
            key=lambda item: item.recorded_at,
            reverse=True,
        )

    def completion_reviews(
        self, *, status: str | None = None
    ) -> list[KnowledgeRevision]:
        """Return Agent completion attempts awaiting or carrying a decision."""
        items = self.ledger.by_kind(KIND_COMPLETION_REVIEW)
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.recorded_at, reverse=True)

    def questions(self, *, open_only: bool = True) -> list[KnowledgeRevision]:
        """Return questions raised by Agents."""
        items = self.ledger.by_kind(KIND_QUESTION)
        if open_only:
            items = [item for item in items if item.status == QUESTION_OPEN]
        return sorted(items, key=lambda item: item.recorded_at, reverse=True)

    def entity_candidates(self, *, unresolved_only: bool = True) -> list[KnowledgeRevision]:
        """Return explicit entity mentions awaiting identity confirmation."""
        items = self.ledger.by_kind(KIND_ENTITY_CANDIDATE)
        if unresolved_only:
            items = [item for item in items if item.status == ENTITY_UNRESOLVED]
        return sorted(items, key=lambda item: item.recorded_at, reverse=True)

    async def decide_proposal(
        self, proposal_id: str, decision: str, *, actor: str = "human", note: str = ""
    ) -> KnowledgeRevision | None:
        """Approve or reject one pending proposal, then continue the loop."""
        proposal = self.ledger.head(proposal_id)
        if proposal is None or proposal.kind != KIND_PROJECT_PROPOSAL:
            return None
        if proposal.status != PROPOSAL_PENDING_REVIEW:
            return proposal
        normalized = decision.strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        status = PROPOSAL_APPROVED if normalized == "approve" else PROPOSAL_REJECTED
        updated = self.ledger.revise(
            proposal_id,
            status=status,
            metadata={"review_actor": actor, "review_note": note},
            reason=f"project proposal {normalized}d by {actor}",
        )
        if status == PROPOSAL_APPROVED:
            await self._route_approved_proposals()
        return self.ledger.head(updated.knowledge_id)

    async def answer_question(
        self, question_id: str, answer: str, *, actor: str = "human"
    ) -> KnowledgeRevision | None:
        """Answer one Agent question and return a waiting Project to its Agent."""
        question = self.ledger.head(question_id)
        if question is None or question.kind != KIND_QUESTION:
            return None
        value = (answer or "").strip()
        if not value:
            raise ValueError("answer must not be empty")
        updated = self.ledger.revise(
            question_id,
            status=QUESTION_ANSWERED,
            metadata={"answer": value, "answered_by": actor},
            reason=f"answered by {actor}",
        )
        project_id = str(question.metadata.get("project_id") or "")
        project = self.orchestrator.projects.get(project_id) if project_id else None
        if project is not None and project.is_live:
            # A human answer is more work for the same Goal, not a new Project.
            # add_task also resolves current blockers and reuses the same Agent.
            await self.orchestrator.add_task(project, f"Human answer: {value}")
            await self.orchestrator.drain()
        return updated

    async def decide_artifact(
        self,
        artifact_id: str,
        decision: str,
        *,
        actor: str = "human",
        note: str = "",
    ) -> KnowledgeRevision | None:
        """Approve one artifact or reject its whole completion attempt.

        A Project completes when every artifact in the attempt is approved.
        Rejecting one artifact rejects the attempt and returns correction work
        to the same Project Agent.
        """
        artifact = self.ledger.head(artifact_id)
        if artifact is None or artifact.kind != KIND_ARTIFACT:
            return None
        normalized = decision.strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        if normalized == "reject" and not note.strip():
            raise ValueError("feedback must not be empty when rejecting an artifact")
        if artifact.status != ARTIFACT_PENDING_REVIEW:
            return artifact

        review_id = str(artifact.metadata.get("completion_review_id") or "")
        if normalized == "reject":
            if review_id:
                await self.decide_completion_review(
                    review_id, "reject", actor=actor, note=note
                )
            else:
                self.ledger.revise(
                    artifact_id,
                    status=ARTIFACT_REJECTED,
                    metadata={"review_actor": actor, "review_note": note},
                    reason=f"artifact rejected by {actor}",
                )
            return self.ledger.head(artifact_id)

        self.ledger.revise(
            artifact_id,
            status=ARTIFACT_APPROVED,
            metadata={"review_actor": actor, "review_note": note},
            reason=f"artifact approved by {actor}",
        )
        review = self.ledger.head(review_id) if review_id else None
        if review is not None and self._all_artifacts_approved(review):
            await self.decide_completion_review(
                review.knowledge_id, "approve", actor=actor, note=note
            )
        return self.ledger.head(artifact_id)

    async def decide_completion_review(
        self,
        review_id: str,
        decision: str,
        *,
        actor: str = "human",
        note: str = "",
    ) -> KnowledgeRevision | None:
        """Approve or reject one complete Agent delivery, idempotently."""
        review = self.ledger.head(review_id)
        if review is None or review.kind != KIND_COMPLETION_REVIEW:
            return None
        normalized = decision.strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        if normalized == "reject" and not note.strip():
            raise ValueError("feedback must not be empty when rejecting deliverables")
        if review.status != ARTIFACT_PENDING_REVIEW:
            return review

        target = ARTIFACT_APPROVED if normalized == "approve" else ARTIFACT_REJECTED
        for artifact_id in review.metadata.get("artifact_ids") or []:
            artifact = self.ledger.head(str(artifact_id))
            if artifact is not None and artifact.status == ARTIFACT_PENDING_REVIEW:
                self.ledger.revise(
                    artifact.knowledge_id,
                    status=target,
                    metadata={"review_actor": actor, "review_note": note},
                    reason=f"completion {normalized}d by {actor}",
                )
        for observation_id in review.metadata.get("observation_ids") or []:
            observation = self.ledger.head(str(observation_id))
            if observation is None or observation.status != ARTIFACT_PENDING_REVIEW:
                continue
            if normalized == "approve":
                value = observation.content.value
                confidence = _confidence(observation.metadata.get("confidence"))
                accepted = (
                    isinstance(value, dict)
                    and bool(value.get("entity"))
                    and bool(value.get("attribute"))
                    and confidence >= 0.8
                )
                self.ledger.revise(
                    observation.knowledge_id,
                    status="ACCEPTED" if accepted else PROPOSAL_PENDING_REVIEW,
                    metadata={"review_actor": actor, "review_note": note},
                    reason=f"completion approved by {actor}",
                )
                if accepted:
                    annotate_world_fact(
                        self.ledger,
                        observation.knowledge_id,
                        entity=str(value["entity"]),
                        attribute=str(value["attribute"]),
                        value=value.get("value"),
                        confidence=confidence,
                        created_by=actor,
                    )
            else:
                self.ledger.revise(
                    observation.knowledge_id,
                    status=ARTIFACT_REJECTED,
                    metadata={"review_actor": actor, "review_note": note},
                    reason=f"completion rejected by {actor}",
                )
        report_id = str(review.metadata.get("report_id") or "")
        report = self.ledger.head(report_id) if report_id else None
        if report is not None and report.status == ARTIFACT_PENDING_REVIEW:
            self.ledger.revise(
                report_id,
                status=target,
                metadata={"review_actor": actor, "review_note": note},
                reason=f"completion {normalized}d by {actor}",
            )
        updated = self.ledger.revise(
            review_id,
            status=target,
            metadata={"review_actor": actor, "review_note": note},
            reason=f"completion {normalized}d by {actor}",
        )
        await self._apply_completion_decision(updated)
        return self.ledger.head(review_id)

    def _all_artifacts_approved(self, review: KnowledgeRevision) -> bool:
        artifact_ids = [str(item) for item in review.metadata.get("artifact_ids") or []]
        return bool(artifact_ids) and all(
            (item := self.ledger.head(artifact_id)) is not None
            and item.status == ARTIFACT_APPROVED
            for artifact_id in artifact_ids
        )

    async def _apply_completion_decision(self, review: KnowledgeRevision) -> bool:
        """Converge one durable Knowledge decision onto its Project."""
        project_id = str(review.metadata.get("project_id") or "")
        message_id = str(review.metadata.get("message_id") or "")
        if not project_id or not message_id:
            return False
        actor = str(review.metadata.get("review_actor") or "human")
        note = str(review.metadata.get("review_note") or "")
        if review.status == ARTIFACT_APPROVED:
            await self.orchestrator.approve_completion(
                project_id, message_id=message_id, actor=actor, note=note
            )
            return True
        if review.status == ARTIFACT_REJECTED:
            await self.orchestrator.reject_completion(
                project_id,
                message_id=message_id,
                actor=actor,
                feedback=note or "成果物を見直してください",
            )
            return True
        return False

    async def _reconcile_completion_decisions(self) -> int:
        count = 0
        for review in self.completion_reviews():
            if review.status in {ARTIFACT_APPROVED, ARTIFACT_REJECTED}:
                count += int(await self._apply_completion_decision(review))
        return count

    def decide_entity(
        self, entity_id: str, decision: str, *, canonical_id: str | None = None,
        actor: str = "human"
    ) -> KnowledgeRevision | None:
        """Confirm an entity identity or state that it is a separate entity."""
        entity = self.ledger.head(entity_id)
        if entity is None or entity.kind != KIND_ENTITY_CANDIDATE:
            return None
        if entity.status != ENTITY_UNRESOLVED:
            return entity
        normalized = decision.strip().lower()
        if normalized not in {"confirm", "separate"}:
            raise ValueError("decision must be confirm or separate")
        if normalized == "confirm" and not (canonical_id or "").strip():
            raise ValueError("canonical_id is required when confirming an entity")
        if normalized == "confirm":
            observation_id = str(entity.metadata.get("observation_id") or "")
            observation = self.ledger.head(observation_id)
            content = observation.content.value if observation is not None else None
            if not isinstance(content, dict) or not content.get("attribute"):
                raise ValueError("entity candidate has no resolvable observation")
            canonical = str(canonical_id).strip()
            self.ledger.relate(
                observation_id,
                Relation(type=RELATION_ABOUT, target=canonical),
            )
            annotate_world_fact(
                self.ledger,
                observation_id,
                entity=canonical,
                attribute=str(content["attribute"]),
                value=content.get("value"),
                confidence=_confidence(content.get("confidence")),
                created_by=actor,
            )
        return self.ledger.revise(
            entity_id,
            status=ENTITY_CONFIRMED if normalized == "confirm" else ENTITY_SEPARATE,
            metadata={"canonical_id": canonical_id, "resolved_by": actor},
            reason=f"entity identity {normalized}ed by {actor}",
        )

    # --- evidence acquisition ------------------------------------------

    def _ingest_manual_events(self) -> int:
        count = 0
        for event in self.runtime.event_store.by_type("knowledge_observed"):
            knowledge_id = _stable_id("manual", str(event.id))
            if self.ledger.head(knowledge_id) is not None:
                continue
            text = str((event.payload or {}).get("text") or "").strip()
            if not text:
                continue
            self.ledger.record(
                text,
                knowledge_id=knowledge_id,
                source_type="manual_observation",
                source_ref=str(event.id),
                metadata={
                    "event_id": str(event.id),
                    "ingress_receipt_id": (
                        str(event.ingress_receipt_id) if event.ingress_receipt_id else None
                    ),
                },
            )
            count += 1
        return count

    def _ingest_source_observations(self) -> int:
        """Turn measured source fields into provenance-bearing World facts."""
        count = 0
        for event in self.runtime.event_store.by_type(SYSTEM_SNAPSHOT_EVENT):
            payload = event.payload or {}
            source_id = str(payload.get("source_id") or "")
            values = payload.get("values")
            if not source_id or not isinstance(values, dict):
                continue
            for attribute, value in flatten_snapshot(values):
                knowledge_id = _stable_id(
                    "system-observation", f"{event.id}:{attribute}"
                )
                if self.ledger.head(knowledge_id) is not None:
                    continue
                recorded = self.ledger.record(
                    value,
                    knowledge_id=knowledge_id,
                    source_type="system_snapshot",
                    source_ref=str(event.id),
                    format="json",
                    kind=KIND_SOURCE_OBSERVATION,
                    relations=[Relation(type=RELATION_ABOUT, target=f"system:{source_id}")],
                    metadata={
                        "source_id": source_id,
                        "event_id": str(event.id),
                        "field": attribute,
                        "ingress_receipt_id": (
                            str(event.ingress_receipt_id)
                            if event.ingress_receipt_id else None
                        ),
                    },
                )
                annotate_world_fact(
                    self.ledger,
                    recorded.knowledge_id,
                    entity=f"system:{source_id}",
                    attribute=attribute,
                    value=value,
                    confidence=1.0,
                    created_by="system_snapshot",
                )
                count += 1
        return count

    def _ingest_resources(self) -> int:
        count = 0
        for representation in self.runtime.resource_store.all_representations():
            knowledge_id = _stable_id("representation", str(representation.id))
            if self.ledger.head(knowledge_id) is not None:
                continue
            version = self.runtime.resource_store.get_version(
                representation.resource_version_id
            )
            resource = (
                self.runtime.resource_store.get_resource(version.resource_id)
                if version is not None
                else None
            )
            self.ledger.record(
                representation.content,
                knowledge_id=knowledge_id,
                source_type="resource_representation",
                source_ref=str(representation.id),
                format="text" if isinstance(representation.content, str) else "json",
                kind=KIND_RESOURCE_OBSERVATION,
                relations=(
                    [Relation(type=RELATION_ABOUT, target=resource.uri)] if resource else []
                ),
                metadata={
                    "resource_id": str(resource.id) if resource else None,
                    "resource_uri": resource.uri if resource else None,
                    "resource_version_id": str(version.id) if version else None,
                    "resource_version": version.version if version else None,
                    "representation_id": str(representation.id),
                    "representation_type": representation.representation_type,
                },
            )
            count += 1
        return count

    def _ingest_agent_messages(self) -> int:
        count = 0
        for direction, message in self.orchestrator.message_store.all():
            if direction != "inbound":
                continue
            report_id = _stable_id("a2a", message.id)
            report = self.ledger.head(report_id)
            if report is None:
                project = self.orchestrator.projects.get(message.project_id or "")
                kind = (
                    KIND_EXPERIENCE
                    if message.type is A2AMessageType.PROJECT_COMPLETED
                    else KIND_AGENT_REPORT
                )
                summary = str(
                    message.payload.get("summary")
                    or message.payload.get("reason")
                    or message.type.value
                )
                report = self.ledger.record(
                    summary,
                    knowledge_id=report_id,
                    source_type="project_agent",
                    source_ref=message.id,
                    kind=kind,
                    status=(
                        ARTIFACT_PENDING_REVIEW
                        if message.type is A2AMessageType.PROJECT_COMPLETED
                        else None
                    ),
                    relations=(
                        [Relation(type=RELATION_ABOUT, target=message.project_id)]
                        if message.project_id
                        else []
                    ),
                    metadata={
                        "message_id": message.id,
                        "message_type": message.type.value,
                        "project_id": message.project_id,
                        "agent_id": message.source_agent_id,
                        "project_goal": project.goal if project else None,
                        "payload": dict(message.payload),
                    },
                )
                count += 1
            # Payload children have their own deterministic ids.  Always
            # reconcile them, even when the report already exists: a crash
            # after the report row but before an artifact row must not lose it.
            self._record_agent_payload(report, message)
        return count

    def _record_agent_payload(
        self, report: KnowledgeRevision, message: A2AMessage
    ) -> None:
        payload = message.payload or {}
        completion_review_id = (
            _stable_id("completion-review", message.id)
            if message.type is A2AMessageType.PROJECT_COMPLETED
            else ""
        )
        artifact_ids: list[str] = []
        for index, artifact in enumerate(payload.get("artifacts") or []):
            if not isinstance(artifact, dict):
                continue
            artifact_id = _stable_id("artifact", f"{message.id}:{index}")
            artifact_ids.append(artifact_id)
            if self.ledger.head(artifact_id) is not None:
                continue
            self.ledger.record(
                artifact.get("content", artifact),
                knowledge_id=artifact_id,
                source_type="project_agent_artifact",
                source_ref=message.id,
                format="text" if isinstance(artifact.get("content"), str) else "json",
                kind=KIND_ARTIFACT,
                status=(
                    ARTIFACT_PENDING_REVIEW if completion_review_id else None
                ),
                derived_from=[report.knowledge_id],
                metadata={
                    "name": artifact.get("name") or f"artifact-{index + 1}",
                    "media_type": artifact.get("media_type"),
                    "project_id": message.project_id,
                    "agent_id": message.source_agent_id,
                    "completion_review_id": completion_review_id or None,
                    "completion_message_id": message.id,
                },
            )

        observation_ids: list[str] = []
        for index, observation in enumerate(payload.get("observations") or []):
            if not isinstance(observation, dict):
                continue
            observation_id = _stable_id("agent-observation", f"{message.id}:{index}")
            observation_ids.append(observation_id)
            recorded = self.ledger.head(observation_id)
            if recorded is None:
                confidence = _confidence(observation.get("confidence"))
                accepted = (
                    not completion_review_id
                    and bool(observation.get("entity"))
                    and bool(observation.get("attribute"))
                    and confidence >= 0.8
                )
                recorded = self.ledger.record(
                    observation,
                    knowledge_id=observation_id,
                    source_type="project_agent_observation",
                    source_ref=message.id,
                    format="json",
                    kind=KIND_AGENT_OBSERVATION,
                    status=(
                        ARTIFACT_PENDING_REVIEW
                        if completion_review_id
                        else "ACCEPTED" if accepted else PROPOSAL_PENDING_REVIEW
                    ),
                    derived_from=[report.knowledge_id],
                    metadata={
                        "project_id": message.project_id,
                        "agent_id": message.source_agent_id,
                        "confidence": confidence,
                    },
                )
                if accepted:
                    annotate_world_fact(
                        self.ledger,
                        recorded.knowledge_id,
                        entity=str(observation["entity"]),
                        attribute=str(observation["attribute"]),
                        value=observation.get("value"),
                        confidence=confidence,
                        created_by=message.source_agent_id,
                    )
            self._record_entity_candidate(
                report, message, observation, index, observation_id
            )

        if completion_review_id and self.ledger.head(completion_review_id) is None:
            self.ledger.record(
                str(payload.get("summary") or "AgentがProjectの完了を報告しました"),
                knowledge_id=completion_review_id,
                source_type="project_agent_completion",
                source_ref=message.id,
                kind=KIND_COMPLETION_REVIEW,
                status=ARTIFACT_PENDING_REVIEW,
                derived_from=[report.knowledge_id, *artifact_ids, *observation_ids],
                metadata={
                    "message_id": message.id,
                    "report_id": report.knowledge_id,
                    "artifact_ids": artifact_ids,
                    "observation_ids": observation_ids,
                    "project_id": message.project_id,
                    "agent_id": message.source_agent_id,
                },
            )

        for index, question in enumerate(payload.get("questions") or []):
            text = question.get("text") if isinstance(question, dict) else question
            if not str(text or "").strip():
                continue
            question_id = _stable_id("question", f"{message.id}:{index}")
            if self.ledger.head(question_id) is not None:
                continue
            self.ledger.record(
                str(text).strip(),
                knowledge_id=question_id,
                source_type="project_agent_question",
                source_ref=message.id,
                kind=KIND_QUESTION,
                status=QUESTION_OPEN,
                derived_from=[report.knowledge_id],
                metadata={
                    "project_id": message.project_id,
                    "agent_id": message.source_agent_id,
                },
            )

    def _record_entity_candidate(
        self,
        report: KnowledgeRevision,
        message: A2AMessage,
        observation: dict[str, Any],
        index: int,
        observation_id: str,
    ) -> None:
        label = str(observation.get("entity_label") or "").strip()
        if not label:
            return
        entity_id = _stable_id("entity", f"{message.id}:{index}:{label}")
        if self.ledger.head(entity_id) is not None:
            return
        self.ledger.record(
            label,
            knowledge_id=entity_id,
            source_type="project_agent_entity_mention",
            source_ref=message.id,
            kind=KIND_ENTITY_CANDIDATE,
            status=ENTITY_UNRESOLVED,
            derived_from=[report.knowledge_id],
            metadata={
                "suggested_entity_id": observation.get("entity"),
                "observation_id": observation_id,
                "project_id": message.project_id,
                "agent_id": message.source_agent_id,
            },
        )

    # --- situation evaluation ------------------------------------------

    #: Kinds a Consolidated Memory may compress, and a Principle generalise from.
    _CONSOLIDATABLE = ("raw", KIND_RESOURCE_OBSERVATION, KIND_SOURCE_OBSERVATION,
                       KIND_AGENT_REPORT, KIND_AGENT_OBSERVATION, KIND_EXPERIENCE)

    async def _consolidate_knowledge(self) -> int:
        """Compress piled-up observations about one subject into one memory.

        Consolidation is how the Ledger stays readable as it grows: without it
        assessment keeps re-reading an ever-longer list of raw scraps.  It is
        deliberately lazy — a subject is only compressed once
        :attr:`consolidation_threshold` observations about it are not yet
        covered by any Consolidated Memory, and at most
        :attr:`consolidations_per_pass` subjects are compressed per tick.

        Needs a reasoning backend: without one the Consolidator would write an
        unsynthesized listing, which is the right answer for an explicit
        request but only noise when produced automatically every tick.
        """
        if self.backend is None:
            return 0
        heads = self.ledger.all_heads()
        covered = {
            knowledge_id
            for item in heads
            if item.kind == KIND_CONSOLIDATED_MEMORY
            for knowledge_id in item.derived_from
        }

        # Group by what each observation is about.  Anything with no subject —
        # a typed-in note, most obviously — is still compressed, under one
        # shared bucket, rather than piling up unread forever.
        groups: dict[str, list[KnowledgeRevision]] = {}
        pending: dict[str, int] = {}
        for item in heads:
            if item.kind not in self._CONSOLIDATABLE and item.kind != KIND_CONSOLIDATED_MEMORY:
                continue
            subject = _subject_of(item)
            groups.setdefault(subject, []).append(item)
            if item.kind != KIND_CONSOLIDATED_MEMORY and item.knowledge_id not in covered:
                pending[subject] = pending.get(subject, 0) + 1

        subjects = sorted(
            (s for s, count in pending.items() if count >= self.consolidation_threshold),
            key=lambda s: (-pending[s], s),
        )[: self.consolidations_per_pass]
        if not subjects:
            return 0

        consolidator = Consolidator(self.ledger, self.backend)
        created = 0
        for subject in subjects:
            # Oldest first, and existing memories about the same subject are
            # eligible too, so a long-running subject compresses recursively
            # instead of growing one unbounded candidate list.
            candidates = sorted(groups[subject], key=lambda item: item.recorded_at)[
                : self.assessment_batch_size
            ]
            memory = await consolidator.consolidate(
                candidates,
                about=None if subject == UNASSIGNED_SUBJECT else subject,
            )
            if memory is not None:
                created += 1
        return created

    async def _extract_principles(self) -> int:
        """Look for one reusable principle across consolidated memory.

        A principle is what makes the past usable on a situation it did not
        come from, so it generalises over *digested* material (consolidated
        memories and recorded experience), never over one raw observation.
        Extraction is skipped entirely when a principle already cites exactly
        this evidence, so a quiet tick costs no LLM call.
        """
        if self.backend is None:
            return 0
        heads = self.ledger.all_heads()
        cases = [
            item
            for item in heads
            if item.kind == KIND_CONSOLIDATED_MEMORY
            or (
                item.kind == KIND_EXPERIENCE
                and item.status not in {ARTIFACT_PENDING_REVIEW, ARTIFACT_REJECTED}
            )
        ]
        if len(cases) < self.principle_threshold:
            return 0
        cases = sorted(cases, key=lambda item: item.recorded_at)[
            : self.assessment_batch_size
        ]

        evidence = {item.knowledge_id for item in cases}
        for principle in heads:
            if principle.kind == KIND_PRINCIPLE and set(principle.derived_from) == evidence:
                return 0  # this exact evidence already produced a principle

        extracted = await PrincipleExtractor(self.ledger, self.backend).extract(cases)
        return 1 if extracted is not None else 0

    def mature_principles(self) -> list[KnowledgeRevision]:
        """Principles that survived enough confirmation to steer a decision.

        A ``candidate`` principle has not been tested against a counterexample
        yet, so it is deliberately withheld from the evaluator's context.
        """
        return [
            item
            for item in self.ledger.by_kind(KIND_PRINCIPLE)
            if item.status in {STATUS_SUPPORTED, STATUS_VALIDATED}
        ]

    def _already_assessed(self):
        """Return a predicate for "this exact revision has been assessed".

        Keyed by **revision**, not by Knowledge id: a corrected or annotated
        object is new information and must be read again — "何度も読み直す".
        Assessments written before revision ids were recorded fall back to a
        time comparison, so upgrading a database does not re-assess its whole
        history at once.
        """
        assessed_revisions: set[str] = set()
        legacy_assessed_at: dict[str, Any] = {}
        for assessment in self.ledger.by_kind(KIND_SITUATION_ASSESSMENT):
            revision_ids = assessment.metadata.get("evidence_revision_ids")
            if revision_ids:
                assessed_revisions.update(revision_ids)
                continue
            for knowledge_id in assessment.metadata.get("evidence_ids", []):
                seen = legacy_assessed_at.get(knowledge_id)
                if seen is None or assessment.recorded_at > seen:
                    legacy_assessed_at[knowledge_id] = assessment.recorded_at

        def already_assessed(item: KnowledgeRevision) -> bool:
            if item.id in assessed_revisions:
                return True
            seen = legacy_assessed_at.get(item.knowledge_id)
            return seen is not None and item.recorded_at <= seen

        return already_assessed

    async def _assess_new_knowledge(self) -> int:
        if self.backend is None:
            return 0
        already_assessed = self._already_assessed()
        candidates = [
            item
            for item in self.ledger.all_heads()
            if not already_assessed(item)
            and item.kind
            in {
                "raw",
                KIND_RESOURCE_OBSERVATION,
                KIND_SOURCE_OBSERVATION,
                KIND_EXPERIENCE,
                KIND_AGENT_REPORT,
                KIND_AGENT_OBSERVATION,
                KIND_ENTITY_CANDIDATE,
                # A Consolidated Memory is evidence in its own right: it is how
                # many older observations stay usable without re-reading each.
                KIND_CONSOLIDATED_MEMORY,
            }
            and not (
                item.kind == KIND_EXPERIENCE
                and item.status in {ARTIFACT_PENDING_REVIEW, ARTIFACT_REJECTED}
            )
            and not (
                item.kind == KIND_ENTITY_CANDIDATE
                and item.status == ENTITY_UNRESOLVED
            )
        ][: self.assessment_batch_size]
        if not candidates:
            return 0

        active_projects = [item.to_routing_dict() for item in self.orchestrator.projects.live()]
        request = BackendRequest(
            instruction=ASSESSMENT_INSTRUCTION,
            context={
                "world_view": self.world_view(),
                "new_evidence": [_evidence_dict(item) for item in candidates],
                "active_projects": active_projects,
                "principles": [_principle_dict(item) for item in self.mature_principles()],
            },
            output_schema=ASSESSMENT_SCHEMA,
            metadata={"kind": "situation_assessment"},
        )
        backend_result = await self.backend.execute(request)
        parsed = backend_result.parsed_output
        if not backend_result.success or not isinstance(parsed, dict):
            logger.warning("situation assessment backend failed: %s", backend_result.error)
            return 0
        raw_proposals = parsed.get("proposals")
        if not isinstance(raw_proposals, list):
            logger.warning("situation assessment did not return a proposals list")
            return 0

        evidence_ids = [item.knowledge_id for item in candidates]
        evidence_revision_ids = [item.id for item in candidates]
        # Identity is the set of *revisions* read, so re-reading a corrected
        # object writes a new assessment instead of colliding with the old one
        # (which would leave the correction permanently unassessed).
        assessment_id = _stable_id("assessment", "|".join(sorted(evidence_revision_ids)))
        if self.ledger.head(assessment_id) is None:
            self.ledger.record(
                parsed,
                knowledge_id=assessment_id,
                source_type="situation_evaluator",
                format="json",
                kind=KIND_SITUATION_ASSESSMENT,
                derived_from=evidence_ids,
                metadata={
                    "evidence_ids": evidence_ids,
                    "evidence_revision_ids": evidence_revision_ids,
                    "model": backend_result.model,
                    "proposal_count": len(raw_proposals),
                },
            )

        known_ids = {item.knowledge_id for item in self.ledger.all_heads()}
        created = 0
        for raw in raw_proposals:
            proposal = self._validate_proposal(raw, known_ids)
            if proposal is None:
                continue
            proposal_id = _proposal_id(proposal)
            if self.ledger.head(proposal_id) is not None:
                continue
            decision = self.policy.decide(proposal)
            evidence = [
                item for item in self.ledger.all_heads()
                if item.knowledge_id in set(proposal["evidence_ids"])
            ]
            self.ledger.record(
                proposal["objective"],
                knowledge_id=proposal_id,
                source_type="situation_evaluator",
                kind=KIND_PROJECT_PROPOSAL,
                status=decision.status,
                derived_from=list(proposal["evidence_ids"]),
                metadata={
                    **proposal,
                    "policy_reason": decision.reason,
                    "assessment_id": assessment_id,
                    "evidence": [_evidence_dict(item) for item in evidence],
                },
            )
            created += 1
        return created

    @staticmethod
    def _validate_proposal(
        raw: Any, known_ids: set[str]
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        objective = str(raw.get("objective") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        evidence_ids = raw.get("evidence_ids")
        risk = str(raw.get("risk") or "").lower()
        if not objective or not reason or not isinstance(evidence_ids, list):
            return None
        evidence = list(dict.fromkeys(str(item) for item in evidence_ids if str(item) in known_ids))
        if not evidence or risk not in {"low", "medium", "high"}:
            return None
        return {
            "objective": objective,
            "reason": reason,
            "evidence_ids": evidence,
            "confidence": _confidence(raw.get("confidence")),
            "risk": risk,
            "read_only": raw.get("read_only") is True,
            "expected_artifacts": [
                str(item) for item in (raw.get("expected_artifacts") or []) if str(item).strip()
            ],
        }

    async def _route_approved_proposals(self) -> int:
        routed = 0
        eligible = {
            PROPOSAL_AUTO_APPROVED,
            PROPOSAL_APPROVED,
        }
        for proposal in reversed(self.proposals()):
            if proposal.status not in eligible:
                continue
            metadata = dict(proposal.metadata)
            decision, project = await self.orchestrator.submit(
                str(metadata.get("objective") or proposal.content.value),
                source="knowledge_runtime",
                request_id=proposal.knowledge_id,
                project_context={
                    "knowledge_proposal_id": proposal.knowledge_id,
                    "evidence_ids": list(metadata.get("evidence_ids") or []),
                    "evidence": list(metadata.get("evidence") or []),
                    "reason": metadata.get("reason"),
                    "risk": metadata.get("risk"),
                    "read_only": metadata.get("read_only"),
                    "expected_artifacts": list(metadata.get("expected_artifacts") or []),
                },
            )
            self.ledger.revise(
                proposal.knowledge_id,
                status=PROPOSAL_ROUTED,
                metadata={
                    "routing_action": decision.action.value,
                    "project_id": project.id if project is not None else None,
                },
                reason="submitted through ProjectOrchestrator",
            )
            routed += 1
        return routed

    # --- durable event wakeups -----------------------------------------

    def _ensure_knowledge_events(self) -> int:
        """Ensure every relevant Knowledge object has one durable change Event."""
        count = 0
        for item in self.ledger.all_heads():
            event_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"nexus-seed:knowledge:{item.id}"
            )
            if self.runtime.event_store.get(event_id) is not None:
                continue
            self.runtime.event_store.append(
                Event(
                    id=event_id,
                    type="knowledge_changed",
                    source="knowledge_runtime",
                    payload={
                        "knowledge_id": item.knowledge_id,
                        "revision": item.revision,
                        "kind": item.kind,
                        "status": item.status,
                    },
                )
            )
            count += 1
        return count


class ReasoningProjectAgent:
    """A bounded in-process Project Agent backed by an ExecutionBackend.

    This is intentionally modest: it can analyse the Project context and
    return structured evidence/artifacts, but it has no direct tool or file
    authority.  A later A2A Agent Runtime can replace it without changing the
    Knowledge loop or Project Orchestrator.
    """

    OUTPUT_SCHEMA = {
        "type": "object",
        "required": ["outcome", "summary"],
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["completed", "need_human_input", "blocked"],
            },
            "summary": {"type": "string"},
            "reason": {"type": ["string", "null"]},
            "observations": {"type": "array", "items": {"type": "object"}},
            "artifacts": {"type": "array", "items": {"type": "object"}},
            "questions": {"type": "array"},
        },
    }

    def __init__(self, backend: ExecutionBackend | None) -> None:
        self.backend = backend

    async def __call__(self, config, envelope) -> list[A2AMessage]:
        if self.backend is None:
            return [
                A2AMessage(
                    type=A2AMessageType.NEED_HUMAN_INPUT,
                    project_id=config.project_id,
                    payload={
                        "reason": "in-process Project Agent needs an enabled LLM backend",
                        "questions": ["LLMを有効化するか、外部Agentを接続してください。"],
                    },
                )
            ]
        result = await self.backend.execute(
            BackendRequest(
                instruction=(
                    "Act as a bounded Project Agent. Work only from the supplied goal "
                    "and evidence. Do not claim an external side effect. Return concrete "
                    "observations and inline artifacts when supported by evidence; ask a "
                    "human when required information is missing. Return JSON only."
                ),
                context={
                    "goal": config.goal,
                    "project_context": config.project_context,
                    "handover": envelope,
                },
                output_schema=self.OUTPUT_SCHEMA,
                metadata={"kind": "in_process_project_agent", "project_id": config.project_id},
            )
        )
        data = result.parsed_output
        if not result.success or not isinstance(data, dict):
            return [
                A2AMessage(
                    type=A2AMessageType.PROJECT_BLOCKED,
                    project_id=config.project_id,
                    payload={"reason": result.error or "Project Agent returned unusable output"},
                )
            ]
        payload = {
            "summary": str(data.get("summary") or ""),
            "reason": data.get("reason"),
            "observations": list(data.get("observations") or []),
            "artifacts": list(data.get("artifacts") or []),
            "questions": list(data.get("questions") or []),
        }
        outcome = data.get("outcome")
        message_type = {
            "completed": A2AMessageType.PROJECT_COMPLETED,
            "need_human_input": A2AMessageType.NEED_HUMAN_INPUT,
            "blocked": A2AMessageType.PROJECT_BLOCKED,
        }.get(outcome, A2AMessageType.PROJECT_BLOCKED)
        return [
            A2AMessage(
                type=message_type,
                project_id=config.project_id,
                payload=payload,
            )
        ]


def _subject_of(item: KnowledgeRevision) -> str:
    """What this Knowledge is about, or the shared bucket when it says nothing."""
    for relation in item.relations:
        if relation.type == RELATION_ABOUT:
            return relation.target
    return UNASSIGNED_SUBJECT


def _stable_id(namespace: str, value: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()[:20]
    return f"K-{namespace}-{digest}"


def _proposal_id(proposal: dict[str, Any]) -> str:
    identity = json.dumps(
        {
            "objective": proposal["objective"],
            "evidence_ids": sorted(proposal["evidence_ids"]),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return _stable_id("project-proposal", identity)


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _evidence_dict(item: KnowledgeRevision) -> dict[str, Any]:
    return {
        "knowledge_id": item.knowledge_id,
        "kind": item.kind,
        "content": item.content.value,
        "source": item.source.to_dict(),
        "recorded_at": item.recorded_at.isoformat(),
        "status": item.status,
    }


def _principle_dict(item: KnowledgeRevision) -> dict[str, Any]:
    """One earned principle, with how much evidence stands behind it."""
    return {
        "knowledge_id": item.knowledge_id,
        "principle": item.content.value,
        "status": item.status,
        "scope": item.metadata.get("scope"),
        "support_count": item.metadata.get("support_count", 0),
        "counterexample_count": item.metadata.get("counterexample_count", 0),
    }


__all__ = [
    "ARTIFACT_APPROVED",
    "ARTIFACT_PENDING_REVIEW",
    "ARTIFACT_REJECTED",
    "ENTITY_CONFIRMED",
    "ENTITY_SEPARATE",
    "ENTITY_UNRESOLVED",
    "KIND_AGENT_OBSERVATION",
    "KIND_AGENT_REPORT",
    "KIND_ARTIFACT",
    "KIND_COMPLETION_REVIEW",
    "KIND_ENTITY_CANDIDATE",
    "KIND_PROJECT_PROPOSAL",
    "KIND_QUESTION",
    "KIND_RESOURCE_OBSERVATION",
    "KIND_SITUATION_ASSESSMENT",
    "KIND_SOURCE_OBSERVATION",
    "KnowledgeLoop",
    "KnowledgeLoopResult",
    "PROPOSAL_APPROVED",
    "PROPOSAL_AUTO_APPROVED",
    "PROPOSAL_FORBIDDEN",
    "PROPOSAL_PENDING_REVIEW",
    "PROPOSAL_REJECTED",
    "PROPOSAL_ROUTED",
    "ProjectProposalPolicy",
    "QUESTION_ANSWERED",
    "QUESTION_OPEN",
    "ReasoningProjectAgent",
]
