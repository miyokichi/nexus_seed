"""Knowledge persistence, retrieval, and meaning-model boundary."""

from .adapters.mvp import ExistingKnowledgeGateway
from .bootstrap_context import (
    CONTEXT_DIR,
    CONTEXT_DOCUMENTS,
    KIND_CONTEXT_DOCUMENT,
    ContextDocuments,
)
from .consolidation import Consolidator, select_candidates
from .experience import DecisionAdvisory, advisories_for, record_agent_experience
from .ledger import KnowledgeLedger
from .models import *  # noqa: F403
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
    "Annotation",
    "CONTEXT_DIR",
    "CONTEXT_DOCUMENTS",
    "Consolidator",
    "ContextDocuments",
    "CounterexampleSearcher",
    "DecisionAdvisory",
    "ExistingKnowledgeGateway",
    "KIND_CONSOLIDATED_MEMORY",
    "KIND_CONTEXT_DOCUMENT",
    "KIND_EXPERIENCE",
    "KIND_PREDICTION",
    "KIND_PRINCIPLE",
    "KIND_RAW",
    "KnowledgeContent",
    "KnowledgeLedger",
    "KnowledgeRevision",
    "KnowledgeSource",
    "PredictionEngine",
    "PrincipleExtractor",
    "Relation",
    "STATUS_CANDIDATE",
    "STATUS_CONFLICT",
    "STATUS_REFINED",
    "STATUS_SUPPORTED",
    "STATUS_VALIDATED",
    "advisories_for",
    "apply_prediction_feedback",
    "evaluate_prediction",
    "record_agent_experience",
    "record_support",
    "refine_principle",
    "select_candidates",
]
