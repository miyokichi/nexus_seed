"""Canonical Intermediate YAML v0.1 for semantic Knowledge ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "0.1"


class CanonicalValidationError(ValueError):
    """Raised when an intermediate document violates the v0.1 contract."""


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """A minimal locator that can lead a reader back to source material."""

    document_id: str
    location: str

    def to_dict(self) -> dict[str, str]:
        return {"document_id": self.document_id, "location": self.location}


@dataclass(frozen=True, slots=True)
class CanonicalEntity:
    """A relation-bearing business concept with scalar properties."""

    id: str
    name: str
    type: str
    aliases: tuple[str, ...] = ()
    properties: Mapping[str, Any] = field(default_factory=dict)
    source: SourceLocation | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "aliases": list(self.aliases),
            "properties": dict(self.properties),
        }
        if self.source is not None:
            data["source"] = self.source.to_dict()
        return data


@dataclass(frozen=True, slots=True)
class CanonicalRelation:
    """A source-asserted directed relation between canonical entities."""

    subject: str
    predicate: str
    object: str
    source: SourceLocation | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
        }
        if self.source is not None:
            data["source"] = self.source.to_dict()
        return data


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    """Validated v0.1 document ready for the Semantica boundary."""

    document_id: str
    source_file: str
    source_location: str
    text: str
    entities: tuple[CanonicalEntity, ...]
    explicit_relations: tuple[CanonicalRelation, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document": {
                "id": self.document_id,
                "source": {
                    "file": self.source_file,
                    "location": self.source_location,
                },
            },
            "content": {"text": self.text},
            "entities": [entity.to_dict() for entity in self.entities],
            "relations": {
                "explicit": [relation.to_dict() for relation in self.explicit_relations]
            },
        }


def canonical_from_mapping(value: Mapping[str, Any]) -> CanonicalDocument:
    """Validate and normalize a decoded Canonical YAML mapping."""

    root = _mapping(value, "document root")
    _only(root, {"schema_version", "document", "content", "entities", "relations"}, "root")
    version = _text(root.get("schema_version"), "schema_version")
    if version != SCHEMA_VERSION:
        raise CanonicalValidationError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {version!r}"
        )

    document = _mapping(root.get("document"), "document")
    _only(document, {"id", "source"}, "document")
    document_id = _text(document.get("id"), "document.id")
    document_source = _mapping(document.get("source"), "document.source")
    _only(document_source, {"file", "location"}, "document.source")
    source_file = _text(document_source.get("file"), "document.source.file")
    source_location = _text(
        document_source.get("location"), "document.source.location"
    )

    content = _mapping(root.get("content"), "content")
    _only(content, {"text"}, "content")
    text = _text(content.get("text"), "content.text")

    raw_entities = root.get("entities")
    if not isinstance(raw_entities, list):
        raise CanonicalValidationError("entities must be a list")
    entities = tuple(
        _entity(item, index=index, document_id=document_id)
        for index, item in enumerate(raw_entities)
    )
    entity_ids = [entity.id for entity in entities]
    if len(entity_ids) != len(set(entity_ids)):
        raise CanonicalValidationError("entity ids must be unique within a document")

    relations = _mapping(root.get("relations"), "relations")
    _only(relations, {"explicit"}, "relations")
    raw_explicit = relations.get("explicit")
    if not isinstance(raw_explicit, list):
        raise CanonicalValidationError("relations.explicit must be a list")
    explicit = tuple(
        _relation(item, index=index, document_id=document_id)
        for index, item in enumerate(raw_explicit)
    )
    known_ids = set(entity_ids)
    for index, relation in enumerate(explicit):
        if relation.subject not in known_ids:
            raise CanonicalValidationError(
                f"relations.explicit[{index}].subject references unknown entity "
                f"{relation.subject!r}"
            )
        if relation.object not in known_ids:
            raise CanonicalValidationError(
                f"relations.explicit[{index}].object references unknown entity "
                f"{relation.object!r}"
            )

    return CanonicalDocument(
        document_id=document_id,
        source_file=source_file,
        source_location=source_location,
        text=text,
        entities=entities,
        explicit_relations=explicit,
        schema_version=version,
    )


def loads_canonical_yaml(text: str) -> CanonicalDocument:
    """Parse and validate one Canonical YAML v0.1 string."""

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("PyYAML is required to load Canonical YAML") from exc
    try:
        decoded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CanonicalValidationError(f"invalid YAML: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise CanonicalValidationError("Canonical YAML root must be a mapping")
    return canonical_from_mapping(decoded)


def load_canonical_yaml(path: str | Path) -> CanonicalDocument:
    """Read and validate one UTF-8 Canonical YAML file."""

    return loads_canonical_yaml(Path(path).read_text(encoding="utf-8"))


def canonical_from_text(
    text: str,
    *,
    document_id: str,
    source_file: str,
    source_location: str = "document",
    entities: tuple[CanonicalEntity, ...] = (),
    explicit_relations: tuple[CanonicalRelation, ...] = (),
) -> CanonicalDocument:
    """Create the plain-text converter boundary without inferring relations."""

    return canonical_from_mapping(
        CanonicalDocument(
            document_id=document_id,
            source_file=source_file,
            source_location=source_location,
            text=text,
            entities=entities,
            explicit_relations=explicit_relations,
        ).to_dict()
    )


def _entity(value: Any, *, index: int, document_id: str) -> CanonicalEntity:
    field_name = f"entities[{index}]"
    item = _mapping(value, field_name)
    _only(item, {"id", "name", "type", "aliases", "properties", "source"}, field_name)
    aliases = item.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        raise CanonicalValidationError(f"{field_name}.aliases must be a list of strings")
    properties = item.get("properties", {})
    if not isinstance(properties, Mapping):
        raise CanonicalValidationError(f"{field_name}.properties must be a mapping")
    source = _source(item.get("source"), f"{field_name}.source", document_id)
    return CanonicalEntity(
        id=_text(item.get("id"), f"{field_name}.id"),
        name=_text(item.get("name"), f"{field_name}.name"),
        type=_text(item.get("type"), f"{field_name}.type"),
        aliases=tuple(alias.strip() for alias in aliases if alias.strip()),
        properties=dict(properties),
        source=source,
    )


def _relation(value: Any, *, index: int, document_id: str) -> CanonicalRelation:
    field_name = f"relations.explicit[{index}]"
    item = _mapping(value, field_name)
    _only(item, {"subject", "predicate", "object", "source"}, field_name)
    return CanonicalRelation(
        subject=_text(item.get("subject"), f"{field_name}.subject"),
        predicate=_text(item.get("predicate"), f"{field_name}.predicate"),
        object=_text(item.get("object"), f"{field_name}.object"),
        source=_source(item.get("source"), f"{field_name}.source", document_id),
    )


def _source(value: Any, field_name: str, document_id: str) -> SourceLocation | None:
    if value is None:
        return None
    item = _mapping(value, field_name)
    _only(item, {"document_id", "location"}, field_name)
    source_document = _text(item.get("document_id"), f"{field_name}.document_id")
    if source_document != document_id:
        raise CanonicalValidationError(
            f"{field_name}.document_id must reference document.id {document_id!r}"
        )
    return SourceLocation(
        document_id=source_document,
        location=_text(item.get("location"), f"{field_name}.location"),
    )


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CanonicalValidationError(f"{field_name} must be a mapping")
    return value


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _only(value: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CanonicalValidationError(f"{field_name} has unknown fields: {unknown}")


__all__ = [
    "SCHEMA_VERSION",
    "CanonicalDocument",
    "CanonicalEntity",
    "CanonicalRelation",
    "CanonicalValidationError",
    "SourceLocation",
    "canonical_from_mapping",
    "canonical_from_text",
    "load_canonical_yaml",
    "loads_canonical_yaml",
]
