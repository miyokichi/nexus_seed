"""Knowledge Runtime — the layer above the Project Orchestrator.

    Knowledge Runtime      "how does NEXUS SEED perceive the world?"
    Project Orchestrator   "what should be done about it?"
    Agent Runtime          "how does it get done?"

The Knowledge Runtime keeps the messy, provenance-bearing record of what
NEXUS SEED has been told, in the order it learned it, and never destroys a
revision.  Everything else — a consolidated memory, a world-state fact, a
principle, a prediction — is a *derived* read over that ledger (Structure on
Read, not Structure on Write).  None of this replaces the six Core primitives
(Event / Process / State / Context / Continuation / Runtime); Knowledge is
domain data, the same way Observation and StateDelta are.
"""

from .models import (
    Annotation,
    KIND_CONSOLIDATED_MEMORY,
    KIND_EXPERIENCE,
    KIND_PREDICTION,
    KIND_PRINCIPLE,
    KIND_RAW,
    KnowledgeContent,
    KnowledgeRevision,
    KnowledgeSource,
    Relation,
    STATUS_CANDIDATE,
    STATUS_CONFLICT,
    STATUS_REFINED,
    STATUS_SUPPORTED,
    STATUS_VALIDATED,
)
from .bootstrap_context import (
    CONTEXT_DIR,
    CONTEXT_DOCUMENTS,
    KIND_CONTEXT_DOCUMENT,
    ContextDocuments,
)
from .consolidation import Consolidator, select_candidates
from .context_assessment import (
    ASSIGNEE_AGENT,
    ASSIGNEE_HUMAN,
    ASSIGNEE_UNKNOWN,
    CANDIDATE_APPROVED,
    CANDIDATE_PENDING_REVIEW,
    CANDIDATE_REJECTED,
    CANDIDATE_ROUTED,
    FINDING_KINDS,
    KIND_CONTEXT_ASSESSMENT,
    KIND_TASK_CANDIDATE,
    ContextAssessor,
    Finding,
    SituationAssessment,
    TaskCandidate,
)
from .experience import DecisionAdvisory, advisories_for, record_agent_experience
from .goal_bridge import GapRiskOpportunityDetector, GoalBridge, Signal
from .autonomous_loop import (
    ARTIFACT_APPROVED,
    ARTIFACT_PENDING_REVIEW,
    ARTIFACT_REJECTED,
    KIND_COMPLETION_REVIEW,
    KIND_SOURCE_OBSERVATION,
    KnowledgeLoop,
    KnowledgeLoopResult,
    ProjectProposalPolicy,
    ReasoningProjectAgent,
)
from .ledger import KnowledgeLedger
from .principles import (
    CounterexampleSearcher,
    PredictionEngine,
    PrincipleExtractor,
    apply_prediction_feedback,
    evaluate_prediction,
    record_support,
    refine_principle,
)

__all__ = [
    "ARTIFACT_APPROVED",
    "ASSIGNEE_AGENT",
    "ASSIGNEE_HUMAN",
    "ASSIGNEE_UNKNOWN",
    "CANDIDATE_APPROVED",
    "CANDIDATE_PENDING_REVIEW",
    "CANDIDATE_REJECTED",
    "CANDIDATE_ROUTED",
    "CONTEXT_DIR",
    "CONTEXT_DOCUMENTS",
    "ContextAssessor",
    "ContextDocuments",
    "FINDING_KINDS",
    "Finding",
    "KIND_CONTEXT_ASSESSMENT",
    "KIND_CONTEXT_DOCUMENT",
    "KIND_TASK_CANDIDATE",
    "SituationAssessment",
    "TaskCandidate",
    "ARTIFACT_PENDING_REVIEW",
    "ARTIFACT_REJECTED",
    "Consolidator",
    "select_candidates",
    "DecisionAdvisory",
    "advisories_for",
    "record_agent_experience",
    "GapRiskOpportunityDetector",
    "GoalBridge",
    "Signal",
    "KnowledgeLoop",
    "KnowledgeLoopResult",
    "ProjectProposalPolicy",
    "ReasoningProjectAgent",
    "KIND_COMPLETION_REVIEW",
    "KIND_SOURCE_OBSERVATION",
    "CounterexampleSearcher",
    "PredictionEngine",
    "PrincipleExtractor",
    "apply_prediction_feedback",
    "evaluate_prediction",
    "record_support",
    "refine_principle",
    "Annotation",
    "KIND_CONSOLIDATED_MEMORY",
    "KIND_EXPERIENCE",
    "KIND_PREDICTION",
    "KIND_PRINCIPLE",
    "KIND_RAW",
    "KnowledgeContent",
    "KnowledgeLedger",
    "KnowledgeRevision",
    "KnowledgeSource",
    "Relation",
    "STATUS_CANDIDATE",
    "STATUS_CONFLICT",
    "STATUS_REFINED",
    "STATUS_SUPPORTED",
    "STATUS_VALIDATED",
]
