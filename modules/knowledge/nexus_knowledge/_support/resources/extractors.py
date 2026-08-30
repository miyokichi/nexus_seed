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
import importlib
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


def _require(module: str, package: str):
    """Import an optional reader, saying what to install when it is missing.

    Office formats need a third-party reader, and NEXUS SEED keeps its runtime
    dependencies minimal, so the import happens here rather than at module
    load.  A missing reader is reported as a normal extraction failure naming
    the fix — the file still becomes a versioned Resource either way, so
    installing the extra later picks its content up on the next change.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExtractionError(
            f"{package} is required to read this format; "
            f"install it with: pip install -e '.[ingest]'"
        ) from exc


class PptxExtractor(_Base):
    """Reads a PowerPoint deck as text, one block per slide."""

    def __init__(self, version: str = "1") -> None:
        super().__init__(
            name="pptx_text",
            version=version,
            representation_type=TEXT,
            resource_types=("pptx",),
        )

    def extract(self, data: bytes) -> str:
        pptx = _require("pptx", "python-pptx")
        try:
            presentation = pptx.Presentation(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001 - any reader failure is the same answer
            raise ExtractionError(f"content is not a readable .pptx: {exc}") from exc
        return "\n\n".join(slide_texts(presentation))


def slide_texts(presentation) -> list[str]:
    """One text block per slide: every text-bearing shape, in slide order."""
    blocks = []
    for index, slide in enumerate(presentation.slides, start=1):
        parts = [
            shape.text_frame.text.strip()
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        if parts:
            blocks.append(f"[slide {index}]\n" + "\n".join(parts))
    return blocks


class XlsxExtractor(_Base):
    """Reads a workbook as ``{sheet: {"columns": [...], "rows": [...]}}``.

    A *structure* representation, like CSV: a spreadsheet is tabular, and a
    reader that wants columns should not have to re-parse prose.  Formulas are
    read as their last cached value, because that is what the file records —
    NEXUS SEED does not recalculate a workbook it only observed.
    """

    def __init__(self, version: str = "1", *, max_rows: int = 1000) -> None:
        super().__init__(
            name="xlsx",
            version=version,
            representation_type=STRUCTURE,
            resource_types=("xlsx",),
        )
        object.__setattr__(self, "max_rows", max_rows)

    def extract(self, data: bytes) -> Any:
        openpyxl = _require("openpyxl", "openpyxl")
        try:
            workbook = openpyxl.load_workbook(
                io.BytesIO(data), read_only=True, data_only=True
            )
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"content is not a readable .xlsx: {exc}") from exc
        try:
            return {name: self._sheet(workbook[name]) for name in workbook.sheetnames}
        finally:
            workbook.close()

    def _sheet(self, sheet) -> dict[str, Any]:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            if row is None or all(cell is None for cell in row):
                continue
            rows.append(["" if cell is None else cell for cell in row])
            if len(rows) > self.max_rows:
                break
        if not rows:
            return {"columns": [], "rows": [], "row_count": 0}
        columns = [str(cell).strip() for cell in rows[0]]
        body = rows[1 : self.max_rows + 1]
        return {
            "columns": columns,
            "rows": [dict(zip(columns, row)) for row in body],
            "row_count": len(body),
            "truncated": len(rows) > self.max_rows,
        }


class DocxExtractor(_Base):
    """Reads a Word document as text: paragraphs, then table cells."""

    def __init__(self, version: str = "1") -> None:
        super().__init__(
            name="docx_text",
            version=version,
            representation_type=TEXT,
            resource_types=("docx",),
        )

    def extract(self, data: bytes) -> str:
        docx = _require("docx", "python-docx")
        try:
            document = docx.Document(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"content is not a readable .docx: {exc}") from exc
        parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)


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
    """The extractors NEXUS SEED ships with.

    Office readers are registered unconditionally even though their libraries
    are optional: an unreadable file then fails with a message naming the
    package to install, rather than silently producing no Knowledge at all.
    """
    return ExtractorRegistry(
        [
            PlainTextExtractor(),
            JSONExtractor(),
            CSVExtractor(),
            PptxExtractor(),
            XlsxExtractor(),
            DocxExtractor(),
        ]
    )


#: Map a file suffix to a coarse resource type (spec §4: not a MIME registry).
SUFFIX_TYPES: dict[str, str] = {
    ".txt": "text",
    ".log": "text",
    ".md": "markdown",
    ".csv": "csv",
    ".json": "json",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".docx": "docx",
}


def resource_type_for(path_or_uri: str) -> str:
    """Guess a coarse resource type from a path or URI suffix."""
    lowered = path_or_uri.lower()
    for suffix, resource_type in SUFFIX_TYPES.items():
        if lowered.endswith(suffix):
            return resource_type
    return "unknown"
