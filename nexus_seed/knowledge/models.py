"""Knowledge — a revision-tracked, provenance-bearing record of what NEXUS SEED
was told, kept distinct from any structured interpretation of it.

Design principle (spec §2): **Structure on Write, never.**  The only fields a
:class:`KnowledgeRevision` requires are the ones every raw scrap of reality
carries no matter its shape — an id, its content, where it came from, when it
was recorded, and what it revises.  ``subject`` / ``predicate`` / ``object`` /
``confidence`` / ``scope`` and the like are never Core fields: an Agent that
needs them derives a Typed View later and attaches it as an
:class:`Annotation`, without touching the original content.

    Git                     Knowledge Runtime
    ------------------      ------------------------------
    blob                    KnowledgeRevision.content
    commit                  KnowledgeRevision
    parent                  KnowledgeRevision.parents
    branch                  competing hypothesis (a sibling knowledge_id
                            linked by a "supports"/"contradicts" Relation)
    merge                   consolidation (KIND_CONSOLIDATED_MEMORY,
                            derived_from)
    merge conflict          STATUS_CONFLICT
    HEAD                    KnowledgeLedger.head(knowledge_id)
    log                     KnowledgeLedger.history(knowledge_id)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.event import utcnow

# --- kinds -------------------------------------------------------------
#
# ``kind`` is not a closed enum — a caller may use any string — but the
# Knowledge Runtime itself only ever writes these.

KIND_RAW = "raw"
KIND_CONSOLIDATED_MEMORY = "consolidated_memory"
KIND_PRINCIPLE = "principle"
KIND_PREDICTION = "prediction"
KIND_EXPERIENCE = "experience"

# --- principle maturity ladder (spec §16) -------------------------------
#
# Not a separate primitive: a status string on a KIND_PRINCIPLE revision.

STATUS_CANDIDATE = "candidate"
STATUS_SUPPORTED = "supported"
STATUS_VALIDATED = "validated"
STATUS_REFINED = "refined"  # narrowed in scope after a counterexample
#: A relation/content disagreement that was deliberately *not* resolved
#: (spec §12).  Never produced by silently picking a side.
STATUS_CONFLICT = "CONFLICT"

# --- well-known annotation kinds ----------------------------------------
#
# An Annotation is how a Typed View attaches to a Knowledge revision without
# rewriting its content.  ``ANNOTATION_WORLD_FACT`` is the one the Knowledge
# Runtime itself understands (see knowledge/projection.py); any other kind is
# free-form and ignored by the ledger.

ANNOTATION_WORLD_FACT = "world_fact"
ANNOTATION_INTERPRETATION = "interpretation"

# --- well-known relation types ------------------------------------------
#
# ``type`` is a free string (spec §3); these are the ones the Knowledge
# Runtime's own code reads structurally.  Anything else is opaque to it and
# simply carried along.

RELATION_ABOUT = "about"
RELATION_SUPPORTS = "supports"
RELATION_CONTRADICTS = "contradicts"
RELATION_SUPERSEDES = "supersedes"


def new_knowledge_id() -> str:
    """Return a fresh, stable identity for a new Knowledge object."""
    return f"K-{uuid.uuid4().hex[:12]}"


def revision_id_for(knowledge_id: str, revision: int) -> str:
    """The identity of one specific revision (e.g. ``K-abc123-v2``)."""
    return f"{knowledge_id}-v{revision}"


@dataclass
class KnowledgeContent:
    """The immutable payload of a Knowledge revision.

    ``format`` says how to read ``value`` (``"text"`` for free prose,
    ``"json"`` for structured input a caller already has) — it is not a
    schema, just enough to know whether to render or parse ``value``.
    """

    value: Any = ""
    format: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return {"format": self.format, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "KnowledgeContent":
        data = data or {}
        return cls(value=data.get("value", ""), format=data.get("format", "text"))


@dataclass
class KnowledgeSource:
    """Where a Knowledge revision came from (provenance, not proof)."""

    type: str
    ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ref": self.ref}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "KnowledgeSource":
        data = data or {}
        return cls(type=data.get("type", "unknown"), ref=data.get("ref"))


@dataclass
class Relation:
    """A link from one Knowledge object to another thing (optional core)."""

    type: str
    target: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "target": self.target}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Relation":
        return cls(type=data["type"], target=data["target"])


@dataclass
class Annotation:
    """An interpretation *about* a Knowledge revision, never a rewrite of it.

    This is where Typed Views live (subject/predicate/object, a world-fact
    reading, a confidence score, …) — generated by whichever Agent needed
    them, attached without disturbing :attr:`KnowledgeRevision.content`.
    """

    kind: str
    value: Any
    created_by: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Annotation":
        return cls(
            kind=data["kind"],
            value=data.get("value"),
            created_by=data.get("created_by"),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else utcnow(),
        )


@dataclass
class KnowledgeRevision:
    """One immutable revision of one Knowledge object.

    Attributes:
        knowledge_id: Stable identity across all revisions of this object.
        revision: 1-based revision number within ``knowledge_id``.
        content: The immutable payload (never rewritten by later revisions
            that only add relations/annotations — see
            :meth:`KnowledgeLedger.annotate`/:meth:`KnowledgeLedger.relate`).
        source: Where this content came from.
        recorded_at: **Transaction time** — when NEXUS SEED learned this.
        valid_from / valid_to: **Valid time** — when this was/])is true in the
            real world.  Both ``None`` means "time-independent, as far as we
            currently know" (spec §4), not "unknown" and not "always".
        parents: The revision_id(s) this revision directly follows.  Empty
            for the first revision of a knowledge_id; more than one only for
            an explicit consolidation-style merge of revision lines.
        relations: Optional links to other Knowledge objects or external
            entities (``about``, ``supports``, ``contradicts``, …).
        annotations: Optional Typed Views / interpretations layered on top.
        kind: What role this object plays (raw / consolidated_memory /
            principle / prediction / experience). Free string; the Runtime
            only special-cases the ones in this module.
        status: Free-form lifecycle marker (e.g. a principle's maturity, or
            ``STATUS_CONFLICT``). ``None`` when the kind has no lifecycle.
        derived_from: Knowledge ids this revision was consolidated/extracted
            from (the "merge parents", as opposed to :attr:`parents` which is
            this object's own revision history).
        metadata: Free bucket for control data that must not become a forced
            schema (generation/depth, novelty, last_consolidated_at, …).
        id: This revision's own identity (``{knowledge_id}-v{revision}``).
        created_at: When this row was written to the ledger (bookkeeping;
            usually equal to ``recorded_at``).
    """

    knowledge_id: str
    content: KnowledgeContent
    source: KnowledgeSource
    revision: int = 1
    recorded_at: datetime = field(default_factory=utcnow)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    parents: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    kind: str = KIND_RAW
    status: str | None = None
    derived_from: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = revision_id_for(self.knowledge_id, self.revision)

    def relations_of(self, type_: str) -> list[Relation]:
        return [r for r in self.relations if r.type == type_]

    def annotations_of(self, kind: str) -> list[Annotation]:
        return [a for a in self.annotations if a.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "knowledge_id": self.knowledge_id,
            "revision": self.revision,
            "content": self.content.to_dict(),
            "source": self.source.to_dict(),
            "recorded_at": self.recorded_at.isoformat(),
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "parents": list(self.parents),
            "relations": [r.to_dict() for r in self.relations],
            "annotations": [a.to_dict() for a in self.annotations],
            "kind": self.kind,
            "status": self.status,
            "derived_from": list(self.derived_from),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }
