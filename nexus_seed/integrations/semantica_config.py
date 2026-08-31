"""Configuration that connects a Semantica Knowledge backend to the Runtime.

Application wiring, like :mod:`nexus_seed.integrations.project_agent_config`:
it reads the environment and, when a snapshot is configured, builds the
Semantica adapter that already lives behind the Knowledge module's public
boundary and injects it into the ordinary Knowledge Gateway::

    NEXUS_SEED_SEMANTICA_SNAPSHOT=./data/semantica-knowledge.json
    NEXUS_SEED_SEMANTICA_ONTOLOGY=./ontology.yaml

With no snapshot configured this module returns ``None`` and NEXUS SEED starts
exactly as it did before Semantica existed.  Three properties are deliberate:

* Semantica is reached only through ``nexus_seed.modules.knowledge`` — the
  composition root imports no Semantica API of its own.
* Semantica stays an optional dependency.  Building the adapter and *querying*
  a snapshot never import the library; only ``ingest`` does, and that is the
  operator's ``nexus-seed-semantica ingest`` step.
* The Planner is never told any of this.  It receives Knowledge items from the
  Knowledge Gateway, whatever backend produced them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..llm_config import load_env_file
from ..modules.knowledge.adapters.mvp import ExistingKnowledgeGateway
from ..modules.knowledge.ledger import KnowledgeLedger
from ..modules.knowledge.semantica import (
    SemanticaKnowledgeAdapter,
    SemanticQueryBackend,
    load_ontology_yaml,
)

logger = logging.getLogger(__name__)


class SemanticaConfigurationError(ValueError):
    """Semantica settings are present but unusable."""


@dataclass(frozen=True, slots=True)
class SemanticaSettings:
    """Where the semantic snapshot lives and what vocabulary constrains it."""

    snapshot_path: Path | None = None
    ontology_path: Path | None = None
    #: Ask Semantica for locally inferred relations during ingest.  Query-side
    #: behaviour is unaffected, so this only matters to the ingest step.
    infer_relations: bool = False

    @property
    def enabled(self) -> bool:
        """Return whether a Semantica Knowledge backend is configured."""

        return self.snapshot_path is not None

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "SemanticaSettings":
        """Load Semantica settings from ``env_file`` and the environment."""

        load_env_file(env_file)
        snapshot = os.environ.get("NEXUS_SEED_SEMANTICA_SNAPSHOT", "").strip()
        ontology = os.environ.get("NEXUS_SEED_SEMANTICA_ONTOLOGY", "").strip()
        if not snapshot:
            if ontology:
                raise SemanticaConfigurationError(
                    "NEXUS_SEED_SEMANTICA_ONTOLOGY is set without "
                    "NEXUS_SEED_SEMANTICA_SNAPSHOT; there is nothing to constrain"
                )
            return cls()
        ontology_path = _resolve(ontology) if ontology else None
        if ontology_path is not None and not ontology_path.is_file():
            raise SemanticaConfigurationError(
                f"NEXUS_SEED_SEMANTICA_ONTOLOGY does not exist: {ontology_path}"
            )
        return cls(
            snapshot_path=_resolve(snapshot),
            ontology_path=ontology_path,
            infer_relations=_read_bool("NEXUS_SEED_SEMANTICA_INFER_RELATIONS", False),
        )


def build_semantic_backend(
    settings: SemanticaSettings | None = None,
    *,
    env_file: str | Path = ".env",
) -> SemanticQueryBackend | None:
    """Build the configured semantic Knowledge backend, or ``None``.

    A snapshot that does not exist yet is not an error: the file appears at the
    operator's first ingest, and until then every query simply finds nothing.
    """

    settings = settings if settings is not None else SemanticaSettings.from_env(env_file)
    if not settings.enabled:
        return None
    assert settings.snapshot_path is not None  # narrowed by `enabled`
    try:
        ontology = (
            load_ontology_yaml(settings.ontology_path)
            if settings.ontology_path is not None
            else None
        )
    except (OSError, ValueError) as exc:
        raise SemanticaConfigurationError(
            f"NEXUS_SEED_SEMANTICA_ONTOLOGY is not a usable ontology: {exc}"
        ) from exc
    logger.info(
        "semantica knowledge backend: snapshot=%s ontology=%s",
        settings.snapshot_path,
        settings.ontology_path or "none",
    )
    return SemanticaKnowledgeAdapter(
        settings.snapshot_path,
        ontology=ontology,
        infer_relations=settings.infer_relations,
    )


def build_knowledge_gateway(
    ledger: KnowledgeLedger,
    *,
    settings: SemanticaSettings | None = None,
    env_file: str | Path = ".env",
    semantic_backend: SemanticQueryBackend | None = None,
) -> ExistingKnowledgeGateway:
    """Return the ordinary Knowledge Gateway, semantic backend attached if configured.

    This is the single place a NEXUS SEED entry point needs to call: with no
    Semantica settings it returns exactly the gateway it always returned.
    """

    backend = (
        semantic_backend
        if semantic_backend is not None
        else build_semantic_backend(settings, env_file=env_file)
    )
    return ExistingKnowledgeGateway(ledger, semantic_backend=backend)


def _resolve(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _read_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SemanticaConfigurationError(f"{name} must be a boolean")


__all__ = [
    "SemanticaConfigurationError",
    "SemanticaSettings",
    "build_knowledge_gateway",
    "build_semantic_backend",
]
