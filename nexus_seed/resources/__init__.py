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
    DocxExtractor,
    ExtractionError,
    Extractor,
    ExtractorRegistry,
    JSONExtractor,
    PlainTextExtractor,
    PptxExtractor,
    XlsxExtractor,
    default_registry,
    resource_type_for,
    slide_texts,
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
    "DocxExtractor",
    "ExtractionError",
    "Extractor",
    "ExtractorRegistry",
    "IndexResult",
    "JSONExtractor",
    "METADATA",
    "PlainTextExtractor",
    "PptxExtractor",
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
    "XlsxExtractor",
    "content_hash",
    "default_registry",
    "file_uri",
    "get_representation_trace",
    "get_resource_trace",
    "resource_type_for",
    "slide_texts",
    "text_hash",
]
