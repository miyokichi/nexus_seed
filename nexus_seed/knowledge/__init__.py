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
from .consolidation import Consolidator, select_candidates
from .experience import DecisionAdvisory, advisories_for, record_agent_experience
from .goal_bridge import GapRiskOpportunityDetector, GoalBridge, Signal
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
    "Consolidator",
    "select_candidates",
    "DecisionAdvisory",
    "advisories_for",
    "record_agent_experience",
    "GapRiskOpportunityDetector",
    "GoalBridge",
    "Signal",
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
