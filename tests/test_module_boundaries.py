"""Physical module ownership keeps the pre-restructure imports compatible."""

from nexus_seed.adapters.manual import ManualAdapter as LegacyManualAdapter
from nexus_seed.ingress.service import IngressService as LegacyIngressService
from nexus_seed.knowledge.ledger import KnowledgeLedger as LegacyKnowledgeLedger
from nexus_seed.mvp.interfaces import KnowledgeGateway as LegacyKnowledgeGateway
from nexus_seed.orchestrator import ProjectOrchestrator as LegacyProjectOrchestrator
from nexus_seed.storage.knowledge_store import KnowledgeStore as LegacyKnowledgeStore
from nexus_seed.storage.orchestrator_store import ProjectStore as LegacyProjectStore

from nexus_seed.modules.knowledge.adapters.sqlite import KnowledgeStore
from nexus_seed.modules.knowledge.ledger import KnowledgeLedger
from nexus_seed.modules.observer.adapters.manual import ManualAdapter
from nexus_seed.modules.observer.ingress.service import IngressService
from nexus_seed.modules.project_manager import ProjectOrchestrator
from nexus_seed.modules.project_manager.adapters.sqlite import ProjectStore
from nexus_seed.platform.contracts.interfaces import KnowledgeGateway


def test_legacy_paths_are_identity_preserving_facades() -> None:
    assert LegacyManualAdapter is ManualAdapter
    assert LegacyIngressService is IngressService
    assert LegacyKnowledgeLedger is KnowledgeLedger
    assert LegacyKnowledgeStore is KnowledgeStore
    assert LegacyProjectOrchestrator is ProjectOrchestrator
    assert LegacyProjectStore is ProjectStore
    assert LegacyKnowledgeGateway is KnowledgeGateway


def test_little_agent_is_not_a_nexus_python_dependency() -> None:
    """The external Agent remains behind A2A, not an import-time dependency."""
    import nexus_seed.integrations.project_agent as integration

    assert "little_agent" not in integration.__dict__
