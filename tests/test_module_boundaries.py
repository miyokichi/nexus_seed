"""Physical module ownership keeps the pre-restructure imports compatible."""

from __future__ import annotations

import ast
from pathlib import Path

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


def test_extracted_modules_do_not_import_nexus_seed_or_each_other() -> None:
    """Capability modules stay behind the NEXUS SEED composition root."""
    root = Path(__file__).resolve().parents[1]
    module_rules = {
        "modules/knowledge": ("nexus_seed",),
        "modules/observer": ("nexus_seed",),
        "modules/planner": (
            "nexus_seed",
            "nexus_knowledge",
            "nexus_project_manager",
        ),
        "modules/project_manager": ("nexus_seed", "nexus_knowledge"),
    }
    violations: list[str] = []
    for module_path, forbidden in module_rules.items():
        for source in (root / module_path).rglob("*.py"):
            if ".venv" in source.parts:
                continue
            tree = ast.parse(
                source.read_text(encoding="utf-8-sig"),
                filename=str(source),
            )
            for node in ast.walk(tree):
                imported = _imported_module(node)
                if imported and any(
                    imported == name or imported.startswith(f"{name}.")
                    for name in forbidden
                ):
                    violations.append(
                        f"{source.relative_to(root)} imports forbidden {imported}"
                    )
    assert violations == []


def _imported_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.ImportFrom):
        return node.module
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    return None
