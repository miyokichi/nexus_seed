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


#: What each part of the composition root may not reach for.  These are the
#: v0.1 direction rules: a source converter does not know the Knowledge
#: Runtime, a Planner does not know which Knowledge backend answered, and no
#: capability module knows a source document format exists.
COMPOSITION_RULES = {
    "nexus_seed/sources": (
        "semantica",
        "nexus_seed.modules.knowledge.ledger",
        "nexus_seed.modules.knowledge.semantica",
        "nexus_seed.modules.knowledge.adapters",
        "nexus_seed.modules.knowledge.projection",
        "nexus_seed.modules.planner",
        "nexus_seed.modules.project_manager",
        "nexus_seed.modules.observer",
    ),
    "nexus_seed/modules/planner": (
        "semantica",
        "nexus_seed.modules.knowledge.semantica",
        "nexus_seed.sources",
    ),
    "nexus_seed/modules/knowledge": ("nexus_seed.sources",),
    "nexus_seed/modules/observer": ("nexus_seed.sources",),
    "nexus_seed/modules/project_manager": ("nexus_seed.sources",),
}


def test_converters_planners_and_modules_keep_their_direction() -> None:
    """The Canonical YAML boundary is a wall, not a naming convention."""
    root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for module_path, forbidden in COMPOSITION_RULES.items():
        for source in (root / module_path).rglob("*.py"):
            for imported in _absolute_imports(source, root):
                if any(
                    imported == name or imported.startswith(f"{name}.")
                    for name in forbidden
                ):
                    violations.append(
                        f"{source.relative_to(root)} imports forbidden {imported}"
                    )
    assert violations == []


def test_the_excel_converter_only_needs_the_canonical_contract() -> None:
    """It may read the Canonical schema; that is the whole point of a boundary."""
    root = Path(__file__).resolve().parents[1]
    imports = _absolute_imports(root / "nexus_seed" / "sources" / "excel.py", root)

    assert "nexus_seed.modules.knowledge.canonical" in imports
    assert not any(
        name.startswith("nexus_seed.modules.knowledge.")
        for name in imports
        if name != "nexus_seed.modules.knowledge.canonical"
    )


def _absolute_imports(source: Path, root: Path) -> list[str]:
    """Return every module ``source`` imports, with relative imports resolved."""
    parts = list(source.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
        package = parts
    else:
        package = parts[:-1]
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if not node.level:
                if node.module:
                    names.append(node.module)
                continue
            base = package[: len(package) - node.level + 1]
            names.append(".".join([*base, node.module] if node.module else base))
    return names
