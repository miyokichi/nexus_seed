"""Append-only persistence for :mod:`nexus_seed.knowledge` — the Knowledge Ledger.

``knowledge_revisions`` is the *only* primary store the Knowledge Runtime
writes to (spec §6): a consolidated memory, a principle, a prediction are all
rows in this same table, distinguished by ``kind`` — never a separate
"Memory DB" / "Principle DB". Rows are inserted, never updated or deleted;
correcting or annotating a Knowledge object always appends a new revision.
"""

from __future__ import annotations

from datetime import datetime

from ..knowledge.models import (
    Annotation,
    KnowledgeContent,
    KnowledgeRevision,
    KnowledgeSource,
    Relation,
)
from .database import Database, dumps, loads


class KnowledgeStore:
    """Reads and writes the append-only Knowledge Ledger."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- writes --------------------------------------------------------

    def append(self, rev: KnowledgeRevision) -> KnowledgeRevision:
        """Persist one revision (insert only)."""
        self.db.execute(
            """
            INSERT INTO knowledge_revisions
                (id, knowledge_id, revision, content_format, content_value,
                 source_type, source_ref, recorded_at, valid_from, valid_to,
                 parents, relations, annotations, kind, status, derived_from,
                 metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rev.id,
                rev.knowledge_id,
                rev.revision,
                rev.content.format,
                dumps(rev.content.value),
                rev.source.type,
                rev.source.ref,
                rev.recorded_at.isoformat(),
                rev.valid_from.isoformat() if rev.valid_from else None,
                rev.valid_to.isoformat() if rev.valid_to else None,
                dumps(rev.parents),
                dumps([r.to_dict() for r in rev.relations]),
                dumps([a.to_dict() for a in rev.annotations]),
                rev.kind,
                rev.status,
                dumps(rev.derived_from),
                dumps(rev.metadata),
                rev.created_at.isoformat(),
            ),
        )
        return rev

    # --- single-object reads --------------------------------------------

    def get_revision_by_id(self, revision_id: str) -> KnowledgeRevision | None:
        row = self.db.query_one(
            "SELECT * FROM knowledge_revisions WHERE id = ?", (revision_id,)
        )
        return self._row(row) if row else None

    def get_revision(self, knowledge_id: str, revision: int) -> KnowledgeRevision | None:
        row = self.db.query_one(
            "SELECT * FROM knowledge_revisions WHERE knowledge_id = ? AND revision = ?",
            (knowledge_id, revision),
        )
        return self._row(row) if row else None

    def head(self, knowledge_id: str) -> KnowledgeRevision | None:
        """The latest revision of ``knowledge_id`` (HEAD)."""
        row = self.db.query_one(
            "SELECT * FROM knowledge_revisions WHERE knowledge_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (knowledge_id,),
        )
        return self._row(row) if row else None

    def head_as_of(self, knowledge_id: str, recorded_at: datetime) -> KnowledgeRevision | None:
        """The revision that was HEAD as of a past **transaction time**."""
        row = self.db.query_one(
            "SELECT * FROM knowledge_revisions WHERE knowledge_id = ? AND recorded_at <= ? "
            "ORDER BY revision DESC LIMIT 1",
            (knowledge_id, recorded_at.isoformat()),
        )
        return self._row(row) if row else None

    def history(self, knowledge_id: str) -> list[KnowledgeRevision]:
        """Every revision of ``knowledge_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM knowledge_revisions WHERE knowledge_id = ? ORDER BY revision ASC",
            (knowledge_id,),
        )
        return [self._row(r) for r in rows]

    # --- ledger-wide reads -----------------------------------------------

    def all_knowledge_ids(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT knowledge_id FROM knowledge_revisions")
        return [r["knowledge_id"] for r in rows]

    def all_heads(self, *, as_of: datetime | None = None) -> list[KnowledgeRevision]:
        """The current HEAD of every Knowledge object, optionally as known
        as-of a past transaction time (spec §10: ``World Model = HEAD``)."""
        if as_of is None:
            rows = self.db.query(
                """
                SELECT r.* FROM knowledge_revisions r
                JOIN (
                    SELECT knowledge_id, MAX(revision) AS max_rev
                    FROM knowledge_revisions GROUP BY knowledge_id
                ) m ON r.knowledge_id = m.knowledge_id AND r.revision = m.max_rev
                """
            )
        else:
            ts = as_of.isoformat()
            rows = self.db.query(
                """
                SELECT r.* FROM knowledge_revisions r
                JOIN (
                    SELECT knowledge_id, MAX(revision) AS max_rev
                    FROM knowledge_revisions
                    WHERE recorded_at <= ?
                    GROUP BY knowledge_id
                ) m ON r.knowledge_id = m.knowledge_id AND r.revision = m.max_rev
                """,
                (ts,),
            )
        return [self._row(r) for r in rows]

    def all_revisions(self) -> list[KnowledgeRevision]:
        """Every revision ever written, in ledger (creation) order."""
        rows = self.db.query("SELECT * FROM knowledge_revisions ORDER BY seq ASC")
        return [self._row(r) for r in rows]

    def by_kind(self, kind: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        heads = self.all_heads() if heads_only else self.all_revisions()
        return [r for r in heads if r.kind == kind]

    def by_status(self, status: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        heads = self.all_heads() if heads_only else self.all_revisions()
        return [r for r in heads if r.status == status]

    def by_source(
        self, source_type: str, ref: str | None = None, *, heads_only: bool = True
    ) -> list[KnowledgeRevision]:
        heads = self.all_heads() if heads_only else self.all_revisions()
        return [
            r
            for r in heads
            if r.source.type == source_type and (ref is None or r.source.ref == ref)
        ]

    def by_relation_target(self, target: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        """Heads (or all revisions) carrying a relation pointing at ``target``."""
        heads = self.all_heads() if heads_only else self.all_revisions()
        return [r for r in heads if any(rel.target == target for rel in r.relations)]

    def derived_from_any(self, knowledge_ids: list[str], *, heads_only: bool = True) -> list[KnowledgeRevision]:
        """Heads whose ``derived_from`` overlaps ``knowledge_ids``."""
        wanted = set(knowledge_ids)
        heads = self.all_heads() if heads_only else self.all_revisions()
        return [r for r in heads if wanted & set(r.derived_from)]

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _row(row) -> KnowledgeRevision:
        return KnowledgeRevision(
            knowledge_id=row["knowledge_id"],
            content=KnowledgeContent(value=loads(row["content_value"]), format=row["content_format"]),
            source=KnowledgeSource(type=row["source_type"], ref=row["source_ref"]),
            revision=row["revision"],
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            valid_from=datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else None,
            valid_to=datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            parents=loads(row["parents"]) or [],
            relations=[Relation.from_dict(d) for d in (loads(row["relations"]) or [])],
            annotations=[Annotation.from_dict(d) for d in (loads(row["annotations"]) or [])],
            kind=row["kind"],
            status=row["status"],
            derived_from=loads(row["derived_from"]) or [],
            metadata=loads(row["metadata"]) or {},
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
