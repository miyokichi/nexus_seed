"""The three files a person writes NEXUS SEED's world in.

They are prose, and they stay prose: these tests are mostly about what is
*not* done to them — no parsing, no schema, and no losing the previous
wording when someone edits a file.
"""

from __future__ import annotations

from nexus_seed.knowledge.bootstrap_context import (
    CONTEXT_DOCUMENTS,
    KIND_CONTEXT_DOCUMENT,
    ContextDocuments,
    context_id,
)
from nexus_seed.knowledge.ledger import KnowledgeLedger
from nexus_seed.storage import Database, KnowledgeStore


def documents(tmp_path):
    db = Database(str(tmp_path / "k.db"))
    ledger = KnowledgeLedger(KnowledgeStore(db))
    return ContextDocuments(ledger, tmp_path / "context"), ledger, db


def test_missing_documents_are_created_with_something_to_type_into(tmp_path):
    docs, _ledger, db = documents(tmp_path)
    try:
        created = docs.ensure()

        assert sorted(path.name for path in created) == sorted(CONTEXT_DOCUMENTS)
        for path in created:
            assert path.read_text(encoding="utf-8").strip()
    finally:
        db.close()


def test_ensure_never_overwrites_what_a_person_wrote(tmp_path):
    docs, _ledger, db = documents(tmp_path)
    try:
        docs.ensure()
        (tmp_path / "context" / "goals.md").write_text("BLOCKEDを放置しない", encoding="utf-8")

        docs.ensure()

        assert (tmp_path / "context" / "goals.md").read_text(encoding="utf-8") == (
            "BLOCKEDを放置しない"
        )
    finally:
        db.close()


def test_the_text_is_recorded_exactly_as_written(tmp_path):
    docs, ledger, db = documents(tmp_path)
    try:
        docs.ensure()
        prose = "# Goal\n\nBLOCKED Projectを放置しない。\n判断に迷ったら人に聞く。\n"
        (tmp_path / "context" / "goals.md").write_text(prose, encoding="utf-8")

        docs.sync()

        head = ledger.head(context_id("goals.md"))
        assert head.content.value == prose
        assert head.kind == KIND_CONTEXT_DOCUMENT
        assert head.source.type == "bootstrap_context"
    finally:
        db.close()


def test_an_unchanged_file_writes_nothing(tmp_path):
    docs, ledger, db = documents(tmp_path)
    try:
        docs.ensure()
        assert len(docs.sync()) == 3

        assert docs.sync() == []
        assert ledger.head(context_id("terms.md")).revision == 1
    finally:
        db.close()


def test_editing_a_file_keeps_what_it_used_to_say(tmp_path):
    docs, ledger, db = documents(tmp_path)
    try:
        docs.ensure()
        goals = tmp_path / "context" / "goals.md"
        goals.write_text("最初の方針", encoding="utf-8")
        docs.sync()
        goals.write_text("あとで変えた方針", encoding="utf-8")

        changed = docs.sync()

        assert [item.knowledge_id for item in changed] == [context_id("goals.md")]
        history = ledger.history(context_id("goals.md"))
        assert [item.content.value for item in history] == [
            "最初の方針",
            "あとで変えた方針",
        ]
    finally:
        db.close()


def test_a_document_that_is_not_there_is_simply_absent(tmp_path):
    docs, _ledger, db = documents(tmp_path)
    try:
        (tmp_path / "context").mkdir()
        (tmp_path / "context" / "goals.md").write_text("方針だけある", encoding="utf-8")

        docs.sync()

        assert sorted(docs.documents()) == ["goals"]
        assert docs.as_context()["goals"]["text"] == "方針だけある"
    finally:
        db.close()


def test_the_revision_key_moves_only_when_the_text_moves(tmp_path):
    docs, _ledger, db = documents(tmp_path)
    try:
        docs.ensure()
        docs.sync()
        before = docs.revision_key()

        docs.sync()
        assert docs.revision_key() == before

        (tmp_path / "context" / "situation.md").write_text("Aが止まった", encoding="utf-8")
        docs.sync()
        assert docs.revision_key() != before
    finally:
        db.close()
