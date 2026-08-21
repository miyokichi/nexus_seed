"""nexus_seed.knowledge.ingest_pptx: turn local .pptx slides into Knowledge.

Uses a real .pptx file built with python-pptx (skipped if the `ingest` extra
is not installed) rather than a fake, since the point of this module is
reading an actual Office format.
"""

from __future__ import annotations

import pytest

pptx = pytest.importorskip("pptx")

from nexus_seed.knowledge import KnowledgeLedger, select_candidates
from nexus_seed.knowledge.ingest_pptx import extract_slide_texts, ingest_pptx_file, main
from nexus_seed.knowledge.models import RELATION_ABOUT
from nexus_seed.storage import Database, KnowledgeStore


def _make_deck(path, slide_texts: list[str]):
    presentation = pptx.Presentation()
    layout = presentation.slide_layouts[1]  # title + content
    for text in slide_texts:
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = text
    presentation.save(str(path))
    return path


def _ledger(tmp_path) -> KnowledgeLedger:
    db = Database(tmp_path / "k.db")
    return KnowledgeLedger(KnowledgeStore(db))


async def test_extract_slide_texts_reads_real_pptx(tmp_path):
    deck = _make_deck(tmp_path / "review.pptx", ["B案の方がmarginはありそう", "process追加が必要"])
    texts = extract_slide_texts(deck)
    assert texts == ["B案の方がmarginはありそう", "process追加が必要"]


async def test_ingest_pptx_file_records_one_knowledge_per_slide(tmp_path):
    ledger = _ledger(tmp_path)
    deck = _make_deck(
        tmp_path / "review_20260820.pptx",
        ["B案の方がmarginはありそう", "process追加が必要でschedule riskが高い"],
    )

    created = ingest_pptx_file(ledger, deck, about="project-A")

    assert len(created) == 2
    assert created[0].content.value == "B案の方がmarginはありそう"
    assert created[0].source.type == "pptx"
    assert created[0].source.ref == "review_20260820.pptx#slide1"
    assert any(r.type == RELATION_ABOUT and r.target == "project-A" for r in created[0].relations)

    # Sources are already eligible for K3 consolidation, grouped by `about`.
    candidates = select_candidates(ledger, about="project-A")
    assert len(candidates) == 2


async def test_ingest_pptx_file_skips_blank_slides(tmp_path):
    ledger = _ledger(tmp_path)
    deck = _make_deck(tmp_path / "mixed.pptx", ["real content", ""])
    created = ingest_pptx_file(ledger, deck)
    assert len(created) == 1
    assert created[0].content.value == "real content"


async def test_cli_main_ingests_a_directory(tmp_path, capsys):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _make_deck(docs_dir / "a.pptx", ["slide one"])
    _make_deck(docs_dir / "b.pptx", ["slide two", "slide three"])
    db_path = tmp_path / "k.db"

    exit_code = main(["--db", str(db_path), "--dir", str(docs_dir)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "total: 3 Knowledge object(s)" in out

    ledger = KnowledgeLedger(KnowledgeStore(Database(db_path)))
    assert len(ledger.by_source("pptx")) == 3  # heads: one per slide, despite 2 revisions each


async def test_cli_main_reports_missing_input(capsys):
    exit_code = main(["--db", "unused.db"])
    assert exit_code == 2
    assert "no .pptx files given" in capsys.readouterr().err
