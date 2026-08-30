"""Semantica adapter behind the :mod:`nexus_knowledge` public boundary."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .canonical import CanonicalDocument, load_canonical_yaml


SNAPSHOT_FORMAT = "nexus_knowledge.semantica.v1"


class SemanticaUnavailable(RuntimeError):
    """Raised when the optional Semantica package is not installed."""


@dataclass(frozen=True, slots=True)
class OntologyVocabulary:
    """Small, extensible allow-list used to constrain the initial graph."""

    version: str
    entity_types: tuple[str, ...]
    relation_types: tuple[str, ...]

    def validate(self, document: CanonicalDocument) -> None:
        """Reject values outside the configured business vocabulary."""

        entity_types = set(self.entity_types)
        relation_types = set(self.relation_types)
        for entity in document.entities:
            if entity.type not in entity_types:
                raise ValueError(f"entity type {entity.type!r} is not in the ontology")
        for relation in document.explicit_relations:
            if relation.predicate not in relation_types:
                raise ValueError(
                    f"relation type {relation.predicate!r} is not in the ontology"
                )


@dataclass(frozen=True, slots=True)
class SemanticKnowledgeContext:
    """Backend-neutral relevant Knowledge returned to NEXUS composition."""

    query: str
    entities: tuple[dict[str, Any], ...]
    explicit_relations: tuple[dict[str, Any], ...]
    inferred_relations: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the compact JSON shape consumed by PlanningContext."""

        return {
            "query": self.query,
            "entities": [dict(entity) for entity in self.entities],
            "properties": {
                str(entity.get("id")): dict(entity.get("properties") or {})
                for entity in self.entities
            },
            "relations": {
                "explicit": [dict(relation) for relation in self.explicit_relations],
                "inferred": [dict(relation) for relation in self.inferred_relations],
            },
            "sources": [dict(source) for source in self.sources],
        }


class SemanticQueryBackend(Protocol):
    """The narrow query contract used by the Knowledge Gateway."""

    def query(self, context_query: str, *, limit: int = 20) -> SemanticKnowledgeContext | None:
        """Return relevant semantic Knowledge, or ``None`` when nothing matches."""


class GraphRuntime(Protocol):
    """Tiny seam around Semantica's graph builder for deterministic tests."""

    def build(
        self,
        source: dict[str, Any],
        *,
        content: str,
        infer_relations: bool,
        relation_types: tuple[str, ...],
    ) -> dict[str, Any]:
        """Build and return a Semantica graph dictionary."""


class SemanticaGraphRuntime:
    """Call Semantica 0.6's in-process ``GraphBuilder`` API."""

    def build(
        self,
        source: dict[str, Any],
        *,
        content: str,
        infer_relations: bool,
        relation_types: tuple[str, ...],
    ) -> dict[str, Any]:
        """Build a graph, optionally adding locally inferred relations."""

        try:
            from semantica.kg import GraphBuilder
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise SemanticaUnavailable(
                "install nexus-knowledge[semantica] to enable the Semantica adapter"
            ) from exc

        builder = GraphBuilder(
            merge_entities=True,
            entity_resolution_strategy="exact",
            resolve_conflicts=False,
        )
        if not infer_relations:
            return builder.build(source)

        graph = builder.build(
            [source, content],
            ner_method="pattern",
            relation_method="pattern",
            triplet_method="pattern",
            extract_relations=True,
            extract_triplets=True,
        )
        allowed = set(relation_types)
        explicit_keys = {
            (relation["source"], relation["type"], relation["target"])
            for relation in source.get("relationships", [])
        }
        relationships = []
        for relation in graph.get("relationships", []):
            item = dict(relation)
            key = (item.get("source"), item.get("type"), item.get("target"))
            metadata = dict(item.get("metadata") or {})
            if key in explicit_keys:
                metadata["assertion"] = "explicit"
            else:
                if item.get("type") not in allowed:
                    continue
                metadata.update(
                    {
                        "assertion": "inferred",
                        "method": "semantica:pattern",
                    }
                )
            item["metadata"] = metadata
            relationships.append(item)
        graph["relationships"] = relationships
        graph.setdefault("metadata", {})["num_relationships"] = len(relationships)
        return graph


class SemanticaKnowledgeAdapter:
    """Ingest Canonical YAML into Semantica and expose normalized queries."""

    def __init__(
        self,
        snapshot_path: str | Path,
        *,
        ontology: OntologyVocabulary | None = None,
        runtime: GraphRuntime | None = None,
        infer_relations: bool = False,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.ontology = ontology
        self.runtime = runtime or SemanticaGraphRuntime()
        self.infer_relations = infer_relations

    def ingest(self, canonical_yaml: str | Path | CanonicalDocument) -> dict[str, Any]:
        """Validate, build, and atomically persist the current semantic graph."""

        document = (
            canonical_yaml
            if isinstance(canonical_yaml, CanonicalDocument)
            else load_canonical_yaml(canonical_yaml)
        )
        if self.ontology is not None:
            self.ontology.validate(document)

        snapshot = self._load_snapshot()
        documents = dict(snapshot.get("documents") or {})
        documents[document.document_id] = document.to_dict()
        graph_source = self._graph_source(documents)
        relation_types = self.ontology.relation_types if self.ontology else ()
        graph = self.runtime.build(
            graph_source,
            content="\n\n".join(
                str(item.get("content", {}).get("text") or "")
                for item in documents.values()
            ),
            infer_relations=self.infer_relations,
            relation_types=relation_types,
        )
        payload = {
            "format": SNAPSHOT_FORMAT,
            "documents": documents,
            "graph": graph,
        }
        self._save_snapshot(payload)
        return graph

    def query(
        self, context_query: str, *, limit: int = 20
    ) -> SemanticKnowledgeContext | None:
        """Return matching entities, their properties, one-hop edges, and sources."""

        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        snapshot = self._load_snapshot()
        graph = snapshot.get("graph") or {}
        entities = [dict(entity) for entity in graph.get("entities", [])]
        relationships = [
            dict(relation) for relation in graph.get("relationships", [])
        ]
        direct_ids = self._matching_entity_ids(
            context_query,
            entities,
            snapshot.get("documents") or {},
        )
        if not direct_ids:
            return None

        related_ids = set(direct_ids)
        selected_relations = []
        for relation in relationships:
            source = str(relation.get("source") or "")
            target = str(relation.get("target") or "")
            if source in direct_ids or target in direct_ids:
                selected_relations.append(relation)
                related_ids.update((source, target))

        selected_entities = tuple(
            entity for entity in entities if str(entity.get("id")) in related_ids
        )[:limit]
        visible_ids = {str(entity.get("id")) for entity in selected_entities}
        selected_relations = [
            relation
            for relation in selected_relations
            if str(relation.get("source")) in visible_ids
            and str(relation.get("target")) in visible_ids
        ]
        explicit = tuple(
            relation
            for relation in selected_relations
            if (relation.get("metadata") or {}).get("assertion") == "explicit"
        )
        inferred = tuple(
            relation
            for relation in selected_relations
            if (relation.get("metadata") or {}).get("assertion") == "inferred"
        )
        return SemanticKnowledgeContext(
            query=context_query,
            entities=selected_entities,
            explicit_relations=explicit,
            inferred_relations=inferred,
            sources=self._sources(selected_entities, selected_relations),
        )

    @staticmethod
    def _graph_source(documents: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        entities: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        for document in documents.values():
            document_source = dict(document["document"]["source"])
            document_id = str(document["document"]["id"])
            for raw_entity in document.get("entities", []):
                entity = dict(raw_entity)
                source = dict(entity.pop("source", {}) or {})
                entity["metadata"] = {
                    "source": source,
                    "document": {"id": document_id, **document_source},
                }
                entities.append(entity)
            for raw_relation in document.get("relations", {}).get("explicit", []):
                relation = dict(raw_relation)
                source = dict(relation.pop("source", {}) or {})
                relationships.append(
                    {
                        "source": relation["subject"],
                        "target": relation["object"],
                        "type": relation["predicate"],
                        "confidence": 1.0,
                        "metadata": {
                            "assertion": "explicit",
                            "source": source,
                            "document": {"id": document_id, **document_source},
                        },
                    }
                )
        return {"entities": entities, "relationships": relationships}

    @staticmethod
    def _matching_entity_ids(
        query: str,
        entities: list[dict[str, Any]],
        documents: Mapping[str, Mapping[str, Any]],
    ) -> set[str]:
        folded = query.casefold().strip()
        terms = {
            term
            for term in re.findall(r"[\w.-]+", folded)
            if len(term) >= 3
        }
        matches: set[str] = set()
        for entity in entities:
            names = [
                str(entity.get("id") or ""),
                str(entity.get("name") or ""),
                *(str(alias) for alias in entity.get("aliases") or []),
            ]
            haystack = " ".join(names).casefold()
            if not folded or any(name.casefold() in folded for name in names if name):
                matches.add(str(entity.get("id")))
            elif any(term in haystack for term in terms):
                matches.add(str(entity.get("id")))

        if matches:
            return matches
        matching_documents = {
            document_id
            for document_id, document in documents.items()
            if any(
                term in str(document.get("content", {}).get("text") or "").casefold()
                for term in terms
            )
        }
        for entity in entities:
            source = (entity.get("metadata") or {}).get("source") or {}
            if source.get("document_id") in matching_documents:
                matches.add(str(entity.get("id")))
        return matches

    @staticmethod
    def _sources(
        entities: tuple[dict[str, Any], ...], relations: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        unique: dict[str, dict[str, Any]] = {}
        for item in (*entities, *relations):
            metadata = item.get("metadata") or {}
            source = dict(metadata.get("source") or {})
            document = dict(metadata.get("document") or {})
            if not source and not document:
                continue
            value = {"document": document, "location": source.get("location")}
            key = json.dumps(value, sort_keys=True, ensure_ascii=False)
            unique[key] = value
        return tuple(unique.values())

    def _load_snapshot(self) -> dict[str, Any]:
        if not self.snapshot_path.exists():
            return {"format": SNAPSHOT_FORMAT, "documents": {}, "graph": {}}
        value = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        if value.get("format") != SNAPSHOT_FORMAT:
            raise ValueError(f"unsupported Semantica snapshot: {value.get('format')!r}")
        return value

    def _save_snapshot(self, value: Mapping[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, self.snapshot_path)


def load_ontology_yaml(path: str | Path) -> OntologyVocabulary:
    """Load the deliberately small v0.1 ontology vocabulary."""

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("PyYAML is required to load ontology YAML") from exc
    decoded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("ontology root must be a mapping")
    version = decoded.get("version")
    entity_types = decoded.get("entity_types")
    relation_types = decoded.get("relation_types")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("ontology.version is required")
    if not isinstance(entity_types, list) or not all(
        isinstance(value, str) and value.strip() for value in entity_types
    ):
        raise ValueError("ontology.entity_types must be a list of strings")
    if not isinstance(relation_types, list) or not all(
        isinstance(value, str) and value.strip() for value in relation_types
    ):
        raise ValueError("ontology.relation_types must be a list of strings")
    return OntologyVocabulary(
        version=version.strip(),
        entity_types=tuple(value.strip() for value in entity_types),
        relation_types=tuple(value.strip() for value in relation_types),
    )


__all__ = [
    "GraphRuntime",
    "OntologyVocabulary",
    "SemanticKnowledgeContext",
    "SemanticQueryBackend",
    "SemanticaGraphRuntime",
    "SemanticaKnowledgeAdapter",
    "SemanticaUnavailable",
    "load_ontology_yaml",
]
