"""Extractors — pure functions from bytes to a Representation's content.

An extractor knows one format and nothing else.  It does not read the database,
does not decide what a document *means* for the world model, and does not
persist anything: it is called by an extraction **Process**, which stages the
result as a normal ProcessResult effect (Invariant 38).  That is why there is
no "PDF Runtime" and never will be — adding a format is adding an entry to a
registry, not a capability to the engine.

Phase 3E ships small, dependency-free formats (text, JSON, CSV).  The point is
the *boundary*, not format coverage: Office and PDF slot into the same registry
later without touching anything above it.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Representation types Phase 3E understands.
TEXT = "text"
STRUCTURE = "structure"
METADATA = "metadata"


class ExtractionError(Exception):
    """The content could not be rendered as the requested representation."""


@runtime_checkable
class Extractor(Protocol):
    """Turns raw content into one representation type."""

    @property
    def name(self) -> str:
        """Stable extractor name, part of a representation's identity."""
        ...

    @property
    def version(self) -> str:
        """Extractor version — a new version yields a *new* representation."""
        ...

    @property
    def representation_type(self) -> str:
        """Which representation type this produces."""
        ...

    def supports(self, resource_type: str) -> bool:
        """Whether this extractor can handle a resource of that type."""
        ...

    def extract(self, data: bytes) -> Any:
        """Render ``data``; raise :class:`ExtractionError` if it cannot."""
        ...


@dataclass(frozen=True)
class _Base:
    name: str
    version: str
    representation_type: str
    resource_types: tuple[str, ...]

    def supports(self, resource_type: str) -> bool:
        return resource_type in self.resource_types


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionError(f"content is not valid UTF-8: {exc}") from exc


class PlainTextExtractor(_Base):
    """Decodes UTF-8 content as a plain-text representation."""

    def __init__(self, version: str = "1") -> None:
        super().__init__(
            name="plain_text",
            version=version,
            representation_type=TEXT,
            resource_types=("text", "csv", "json", "markdown", "unknown"),
        )

    def extract(self, data: bytes) -> str:
        return _decode(data)


class JSONExtractor(_Base):
    """Parses JSON content into a structure representation."""

    def __init__(self, version: str = "1") -> None:
        super().__init__(
            name="json",
            version=version,
            representation_type=STRUCTURE,
            resource_types=("json",),
        )

    def extract(self, data: bytes) -> Any:
        try:
            return json.loads(_decode(data))
        except ValueError as exc:
            raise ExtractionError(f"content is not valid JSON: {exc}") from exc


class CSVExtractor(_Base):
    """Parses CSV content into ``{"columns": [...], "rows": [...]}``.

    Kept as a *structure* representation distinct from the file's text: the
    same version legitimately has both, and a process that wants columns should
    not have to re-parse a string.
    """

    def __init__(self, version: str = "1") -> None:
        super().__init__(
            name="csv",
            version=version,
            representation_type=STRUCTURE,
            resource_types=("csv",),
        )

    def extract(self, data: bytes) -> Any:
        text = _decode(data)
        try:
            reader = csv.reader(io.StringIO(text))
            rows = [row for row in reader if row]
        except csv.Error as exc:
            raise ExtractionError(f"content is not valid CSV: {exc}") from exc
        if not rows:
            return {"columns": [], "rows": []}
        columns = [c.strip() for c in rows[0]]
        return {
            "columns": columns,
            "rows": [dict(zip(columns, row)) for row in rows[1:]],
            "row_count": len(rows) - 1,
        }


class ExtractorRegistry:
    """``(representation_type, resource_type) -> extractor``, deterministic.

    Ordered, first-match-wins lookup — no scoring, no discovery.  Adding Office
    or PDF support later means registering an extractor here.
    """

    def __init__(self, extractors: list[Extractor] | None = None) -> None:
        self._extractors: list[Extractor] = list(extractors or [])

    def register(self, extractor: Extractor) -> Extractor:
        """Register ``extractor`` (later registrations take precedence)."""
        self._extractors.insert(0, extractor)
        return extractor

    def find(self, representation_type: str, resource_type: str) -> Extractor | None:
        """Return the extractor for a (representation, resource) pair."""
        for extractor in self._extractors:
            if (
                extractor.representation_type == representation_type
                and extractor.supports(resource_type)
            ):
                return extractor
        return None

    def get(self, name: str) -> Extractor | None:
        """Return an extractor by name."""
        for extractor in self._extractors:
            if extractor.name == name:
                return extractor
        return None

    def representation_types_for(self, resource_type: str) -> list[str]:
        """Return every representation type available for a resource type."""
        seen: list[str] = []
        for extractor in self._extractors:
            if extractor.supports(resource_type) and extractor.representation_type not in seen:
                seen.append(extractor.representation_type)
        return seen

    def __len__(self) -> int:
        return len(self._extractors)


def default_registry() -> ExtractorRegistry:
    """The extractors Phase 3E ships with."""
    return ExtractorRegistry([PlainTextExtractor(), JSONExtractor(), CSVExtractor()])


#: Map a file suffix to a coarse resource type (spec §4: not a MIME registry).
SUFFIX_TYPES: dict[str, str] = {
    ".txt": "text",
    ".log": "text",
    ".md": "markdown",
    ".csv": "csv",
    ".json": "json",
}


def resource_type_for(path_or_uri: str) -> str:
    """Guess a coarse resource type from a path or URI suffix."""
    lowered = path_or_uri.lower()
    for suffix, resource_type in SUFFIX_TYPES.items():
        if lowered.endswith(suffix):
            return resource_type
    return "unknown"
