"""Artifact / Resource layer — the documents the world is made of.

    Resource            what a thing IS
      ResourceVersion   what it CONTAINED at a time
        Representation  what we MADE of that content

Domain data plus one shared path boundary (:class:`ResourceScope`); no core
primitives, no document-management system.
"""

from .extractors import (
    METADATA,
    STRUCTURE,
    TEXT,
    CSVExtractor,
    ExtractionError,
    Extractor,
    ExtractorRegistry,
    JSONExtractor,
    PlainTextExtractor,
    default_registry,
    resource_type_for,
)
from .models import (
    Resource,
    ResourceRepresentation,
    ResourceVersion,
    content_hash,
    text_hash,
)
from .scope import ResourceScope, ScopeViolation
from .service import IndexResult, ResourceService, file_uri
from .trace import (
    RepresentationTrace,
    ResourceTrace,
    get_representation_trace,
    get_resource_trace,
)

__all__ = [
    "CSVExtractor",
    "ExtractionError",
    "Extractor",
    "ExtractorRegistry",
    "IndexResult",
    "JSONExtractor",
    "METADATA",
    "PlainTextExtractor",
    "RepresentationTrace",
    "Resource",
    "ResourceRepresentation",
    "ResourceScope",
    "ResourceService",
    "ResourceTrace",
    "ResourceVersion",
    "STRUCTURE",
    "ScopeViolation",
    "TEXT",
    "content_hash",
    "default_registry",
    "file_uri",
    "get_representation_trace",
    "get_resource_trace",
    "resource_type_for",
    "text_hash",
]
