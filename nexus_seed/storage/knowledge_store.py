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

    #: Selects the newest revision of every Knowledge object.  A caller may
    #: append its own ``AND ...`` against ``r`` — the filter is applied *after*
    #: head selection, so ``by_kind("principle")`` means "objects whose current
    #: revision is a principle", never "objects that were ever a principle".
    _HEADS = """
        SELECT r.* FROM knowledge_revisions r
        JOIN (
            SELECT knowledge_id, MAX(revision) AS max_rev
            FROM knowledge_revisions {as_of}GROUP BY knowledge_id
        ) m ON r.knowledge_id = m.knowledge_id AND r.revision = m.max_rev
    """

    #: Same head selection, but narrowed by an indexed predicate *before*
    #: deciding which revision is current.  Cheaper than :attr:`_HEADS`
    #: whenever the predicate is selective, because grouping the whole table
    #: costs O(ledger) no matter how few rows finally match.
    _HEADS_WHERE = """
        SELECT r.* FROM knowledge_revisions r
        WHERE {where}
          AND r.revision = (
              SELECT MAX(h.revision) FROM knowledge_revisions h
              WHERE h.knowledge_id = r.knowledge_id
          )
    """

    def _heads(self, where: str = "", params: tuple = ()) -> list[KnowledgeRevision]:
        sql = (
            self._HEADS_WHERE.format(where=where)
            if where
            else self._HEADS.format(as_of="")
        )
        return [self._row(row) for row in self.db.query(sql, params)]

    def all_heads(self, *, as_of: datetime | None = None) -> list[KnowledgeRevision]:
        """The current HEAD of every Knowledge object, optionally as known
        as-of a past transaction time (spec §10: ``World Model = HEAD``)."""
        if as_of is None:
            return self._heads()
        sql = self._HEADS.format(as_of="WHERE recorded_at <= ? ")
        return [self._row(row) for row in self.db.query(sql, (as_of.isoformat(),))]

    def heads_since(
        self, seq: int, *, kinds: tuple[str, ...] | None = None
    ) -> list[tuple[int, KnowledgeRevision]]:
        """``(seq, head)`` pairs written after ledger position ``seq``, oldest first.

        Lets a caller process only what is new instead of re-reading the whole
        Ledger each pass.  ``seq`` is a position in the append-only table, so a
        lost or reset cursor costs a re-scan, never a missed revision — which
        is why a caller may keep the cursor in memory.
        """
        # Narrow by ledger position *first* — ``seq`` is the rowid, so this is
        # a range scan over just the new rows — then confirm each one is still
        # its object's head with an indexed lookup.  Grouping the whole table
        # first would instead cost O(ledger) on every quiet pass.
        where = "r.seq > ?"
        params: tuple = (seq,)
        if kinds:
            where += f" AND r.kind IN ({','.join('?' * len(kinds))})"
            params += tuple(kinds)
        rows = self.db.query(
            f"""
            SELECT r.* FROM knowledge_revisions r
            WHERE {where}
              AND r.revision = (
                  SELECT MAX(h.revision) FROM knowledge_revisions h
                  WHERE h.knowledge_id = r.knowledge_id
              )
            ORDER BY r.seq ASC
            """,
            params,
        )
        return [(int(row["seq"]), self._row(row)) for row in rows]

    def heads_of_kinds(self, kinds: tuple[str, ...]) -> list[KnowledgeRevision]:
        """Every head whose current revision is one of ``kinds``."""
        if not kinds:
            return []
        placeholders = ",".join("?" * len(kinds))
        return self._heads(f"r.kind IN ({placeholders})", tuple(kinds))

    def max_seq(self) -> int:
        """The current end of the append-only ledger (0 when it is empty)."""
        row = self.db.query_one("SELECT MAX(seq) AS seq FROM knowledge_revisions")
        return int(row["seq"]) if row and row["seq"] is not None else 0

    def all_revisions(self) -> list[KnowledgeRevision]:
        """Every revision ever written, in ledger (creation) order."""
        rows = self.db.query("SELECT * FROM knowledge_revisions ORDER BY seq ASC")
        return [self._row(r) for r in rows]

    def by_kind(self, kind: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        if heads_only:
            return self._heads("r.kind = ?", (kind,))
        return [r for r in self.all_revisions() if r.kind == kind]

    def by_status(self, status: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        if heads_only:
            return self._heads("r.status = ?", (status,))
        return [r for r in self.all_revisions() if r.status == status]

    def by_source(
        self, source_type: str, ref: str | None = None, *, heads_only: bool = True
    ) -> list[KnowledgeRevision]:
        if heads_only:
            if ref is None:
                return self._heads("r.source_type = ?", (source_type,))
            return self._heads(
                "r.source_type = ? AND r.source_ref = ?", (source_type, ref)
            )
        return [
            r
            for r in self.all_revisions()
            if r.source.type == source_type and (ref is None or r.source.ref == ref)
        ]

    def by_relation_target(self, target: str, *, heads_only: bool = True) -> list[KnowledgeRevision]:
        """Heads (or all revisions) carrying a relation pointing at ``target``.

        Relations live in a JSON column, so this one filters in Python rather
        than risking a ``LIKE`` against escaped JSON.  Keep it off hot paths.
        """
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
