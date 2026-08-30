"""KnowledgeLedger — the one primary store behind the Knowledge Runtime.

Domain logic over :class:`~nexus_seed.storage.knowledge_store.KnowledgeStore`:
append new Knowledge, revise it (new revision, old one kept), attach relations
and annotations without touching content, and answer bitemporal queries.
Nothing here calls an LLM — Phase K1 is pure, deterministic ledger mechanics
(spec Phase K1: "まだLLMは使わない").
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ...core.event import utcnow
from .models import (
    Annotation,
    KnowledgeContent,
    KnowledgeRevision,
    KnowledgeSource,
    Relation,
    new_knowledge_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a storage<->domain cycle
    from .adapters.sqlite import KnowledgeStore

_UNSET = object()


class KnowledgeLedger:
    """The single source of truth for everything NEXUS SEED has been told."""

    def __init__(self, store: "KnowledgeStore") -> None:
        self.store = store

    # --- writes ----------------------------------------------------------

    def record(
        self,
        value: Any,
        *,
        source_type: str,
        source_ref: str | None = None,
        format: str = "text",
        knowledge_id: str | None = None,
        recorded_at: datetime | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        relations: list[Relation] | None = None,
        annotations: list[Annotation] | None = None,
        kind: str = "raw",
        status: str | None = None,
        derived_from: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeRevision:
        """Append a brand-new Knowledge object (revision 1).

        ``knowledge_id`` may be supplied to control the identity (e.g. a
        deterministic id derived from an external key); otherwise one is
        generated.  Recording under an id that already has revisions is
        rejected — use :meth:`revise` for that (an id is never silently
        reused for unrelated content).
        """
        knowledge_id = knowledge_id or new_knowledge_id()
        if self.store.head(knowledge_id) is not None:
            raise ValueError(
                f"knowledge_id {knowledge_id!r} already has revisions; use revise()"
            )
        rev = KnowledgeRevision(
            knowledge_id=knowledge_id,
            content=KnowledgeContent(value=value, format=format),
            source=KnowledgeSource(type=source_type, ref=source_ref),
            revision=1,
            recorded_at=recorded_at or utcnow(),
            valid_from=valid_from,
            valid_to=valid_to,
            parents=[],
            relations=list(relations or []),
            annotations=list(annotations or []),
            kind=kind,
            status=status,
            derived_from=list(derived_from or []),
            metadata=dict(metadata or {}),
        )
        return self.store.append(rev)

    def revise(
        self,
        knowledge_id: str,
        *,
        value: Any = _UNSET,
        source_type: str | None = None,
        source_ref: str | None = None,
        format: str | None = None,
        recorded_at: datetime | None = None,
        valid_from: Any = _UNSET,
        valid_to: Any = _UNSET,
        status: Any = _UNSET,
        metadata: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> KnowledgeRevision:
        """Append a new revision of an existing Knowledge object.

        The previous revision is never modified — this only ever adds a row.
        Fields left as their sentinel default carry over unchanged from the
        current HEAD; pass a field explicitly (including ``None``) to change
        it.  This is how late-arriving evidence is handled (spec §4): the
        caller passes a ``valid_from`` in the past while ``recorded_at``
        defaults to now, so the ledger records *when we learned* something
        that was *already true* earlier.
        """
        current = self.store.head(knowledge_id)
        if current is None:
            raise ValueError(f"knowledge_id {knowledge_id!r} has no revisions to revise")

        new_content = current.content if value is _UNSET else KnowledgeContent(
            value=value, format=format or current.content.format
        )
        new_metadata = dict(current.metadata)
        if metadata:
            new_metadata.update(metadata)
        if reason:
            new_metadata["revision_reason"] = reason

        rev = KnowledgeRevision(
            knowledge_id=knowledge_id,
            content=new_content,
            source=KnowledgeSource(
                type=source_type or current.source.type,
                ref=source_ref if source_ref is not None else current.source.ref,
            ),
            revision=current.revision + 1,
            recorded_at=recorded_at or utcnow(),
            valid_from=current.valid_from if valid_from is _UNSET else valid_from,
            valid_to=current.valid_to if valid_to is _UNSET else valid_to,
            parents=[current.id],
            relations=list(current.relations),
            annotations=list(current.annotations),
            kind=current.kind,
            status=current.status if status is _UNSET else status,
            derived_from=list(current.derived_from),
            metadata=new_metadata,
        )
        return self.store.append(rev)

    def annotate(
        self,
        knowledge_id: str,
        annotation: Annotation,
        *,
        recorded_at: datetime | None = None,
    ) -> KnowledgeRevision:
        """Attach a Typed View / interpretation without rewriting content.

        Content is carried over byte-for-byte from HEAD; only the annotation
        list grows.  This is the write path for "Structure on Read" — an
        Agent derives ``subject``/``predicate``/``confidence``/whatever it
        needs and records that reading here, leaving the original words
        untouched for the next reader to interpret differently.
        """
        current = self.store.head(knowledge_id)
        if current is None:
            raise ValueError(f"knowledge_id {knowledge_id!r} has no revisions to annotate")
        rev = KnowledgeRevision(
            knowledge_id=knowledge_id,
            content=current.content,
            source=current.source,
            revision=current.revision + 1,
            recorded_at=recorded_at or utcnow(),
            valid_from=current.valid_from,
            valid_to=current.valid_to,
            parents=[current.id],
            relations=list(current.relations),
            annotations=[*current.annotations, annotation],
            kind=current.kind,
            status=current.status,
            derived_from=list(current.derived_from),
            metadata=dict(current.metadata),
        )
        return self.store.append(rev)

    def relate(
        self,
        knowledge_id: str,
        relation: Relation,
        *,
        recorded_at: datetime | None = None,
    ) -> KnowledgeRevision:
        """Attach a relation to another Knowledge object or entity."""
        current = self.store.head(knowledge_id)
        if current is None:
            raise ValueError(f"knowledge_id {knowledge_id!r} has no revisions to relate")
        rev = KnowledgeRevision(
            knowledge_id=knowledge_id,
            content=current.content,
            source=current.source,
            revision=current.revision + 1,
            recorded_at=recorded_at or utcnow(),
            valid_from=current.valid_from,
            valid_to=current.valid_to,
            parents=[current.id],
            relations=[*current.relations, relation],
            annotations=list(current.annotations),
            kind=current.kind,
            status=current.status,
            derived_from=list(current.derived_from),
            metadata=dict(current.metadata),
        )
        return self.store.append(rev)

    def mark_conflict(self, knowledge_id_a: str, knowledge_id_b: str, *, reason: str) -> None:
        """Record that two Knowledge objects disagree, without resolving it.

        Both sides keep their own content; each gets a ``contradicts``
        relation to the other and its status becomes :data:`STATUS_CONFLICT`
        (spec §12 — a conflict is *held*, not collapsed to one answer).
        """
        from .models import RELATION_CONTRADICTS, STATUS_CONFLICT

        self.relate(knowledge_id_a, Relation(type=RELATION_CONTRADICTS, target=knowledge_id_b))
        self.relate(knowledge_id_b, Relation(type=RELATION_CONTRADICTS, target=knowledge_id_a))
        self.revise(knowledge_id_a, status=STATUS_CONFLICT, reason=reason)
        self.revise(knowledge_id_b, status=STATUS_CONFLICT, reason=reason)

    # --- single-object reads -----------------------------------------------

    def head(self, knowledge_id: str) -> KnowledgeRevision | None:
        """The current revision (HEAD) of one Knowledge object."""
        return self.store.head(knowledge_id)

    def history(self, knowledge_id: str) -> list[KnowledgeRevision]:
        """Every revision of one Knowledge object, oldest first."""
        return self.store.history(knowledge_id)

    def at_revision(self, knowledge_id: str, revision: int) -> KnowledgeRevision | None:
        """A specific historical revision."""
        return self.store.get_revision(knowledge_id, revision)

    def as_known_at(self, knowledge_id: str, recorded_at: datetime) -> KnowledgeRevision | None:
        """What NEXUS SEED believed about ``knowledge_id`` as of a past
        **transaction time** — i.e. ignoring anything learned after
        ``recorded_at``, even if it describes something earlier."""
        return self.store.head_as_of(knowledge_id, recorded_at)

    def valid_at(self, knowledge_id: str, valid_time: datetime) -> KnowledgeRevision | None:
        """The revision whose **valid time** window covers ``valid_time``,
        picking the most recently recorded one if several qualify (the
        current best understanding of what was true then)."""
        candidates = [
            rev
            for rev in self.store.history(knowledge_id)
            if (rev.valid_from is None or rev.valid_from <= valid_time)
            and (rev.valid_to is None or valid_time < rev.valid_to)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.recorded_at)

    # --- ledger-wide reads ---------------------------------------------------

    def all_heads(self, *, as_of: datetime | None = None) -> list[KnowledgeRevision]:
        """HEAD of every Knowledge object — the current World View's raw
        material — optionally as known at a past transaction time."""
        return self.store.all_heads(as_of=as_of)

    def by_kind(self, kind: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        return self.store.by_kind(kind, heads_only=heads_only)

    def by_status(self, status: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        return self.store.by_status(status, heads_only=heads_only)

    def by_source(
        self, source_type: str, ref: str | None = None, *, heads_only: bool = True
    ) -> list[KnowledgeRevision]:
        return self.store.by_source(source_type, ref, heads_only=heads_only)

    def referencing(self, target: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        """Heads carrying a relation that points at ``target``."""
        return self.store.by_relation_target(target, heads_only=heads_only)

    def derived_from_any(
        self, knowledge_ids: list[str], *, heads_only: bool = True
    ) -> list[KnowledgeRevision]:
        return self.store.derived_from_any(knowledge_ids, heads_only=heads_only)
