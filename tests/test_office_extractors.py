"""Office files become Knowledge through the ordinary Resource pipeline.

The file observer already watched, versioned and hashed a .pptx or .xlsx; what
it could not do was read one, so the change was noticed and its content never
arrived. These tests cover the extractors that close that gap, and the whole
path from a file appearing in an authorized folder to its content being
readable as Knowledge.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pptx = pytest.importorskip("pptx")
openpyxl = pytest.importorskip("openpyxl")
docx = pytest.importorskip("docx")

from nexus_seed.adapters import LocalFileAdapter
from nexus_seed.knowledge.autonomous_loop import KIND_RESOURCE_OBSERVATION, KnowledgeLoop
from nexus_seed.orchestrator import InProcessAgentRuntime, ProjectOrchestrator
from nexus_seed.processes.resources import bootstrap_observer, bootstrap_resources
from nexus_seed.resources.extractors import (
    DocxExtractor,
    ExtractionError,
    PptxExtractor,
    XlsxExtractor,
    default_registry,
    resource_type_for,
)
from nexus_seed.resources.scope import ResourceScope
from nexus_seed.runtime.clock import ManualClock
from nexus_seed.runtime.runtime import Runtime


def make_pptx(path, slides):
    presentation = pptx.Presentation()
    for text in slides:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = text
    presentation.save(str(path))
    return path.read_bytes()


def make_xlsx(path, sheets):
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    workbook.save(str(path))
    return path.read_bytes()


def make_docx(path, paragraphs, table=None):
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        added = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                added.cell(r, c).text = value
    document.save(str(path))
    return path.read_bytes()


# --- suffix mapping ----------------------------------------------------------


def test_office_suffixes_are_no_longer_unknown():
    assert resource_type_for("deck.pptx") == "pptx"
    assert resource_type_for("book.xlsx") == "xlsx"
    assert resource_type_for("book.xlsm") == "xlsx"
    assert resource_type_for("memo.docx") == "docx"
    assert resource_type_for("mystery.bin") == "unknown"


def test_registry_resolves_an_extractor_for_each_office_type():
    registry = default_registry()
    assert registry.find("text", "pptx").name == "pptx_text"
    assert registry.find("structure", "xlsx").name == "xlsx"
    assert registry.find("text", "docx").name == "docx_text"


# --- pptx --------------------------------------------------------------------


def test_pptx_extractor_reads_every_slide(tmp_path):
    data = make_pptx(tmp_path / "d.pptx", ["Q3の売上が計画を下回っている", "対策は次回検討"])
    text = PptxExtractor().extract(data)
    assert "Q3の売上が計画を下回っている" in text
    assert "対策は次回検討" in text
    assert "[slide 1]" in text and "[slide 2]" in text


def test_pptx_extractor_rejects_something_that_is_not_a_deck():
    with pytest.raises(ExtractionError, match="not a readable .pptx"):
        PptxExtractor().extract(b"this is not a pptx at all")


# --- xlsx --------------------------------------------------------------------


def test_xlsx_extractor_reads_sheets_as_columns_and_rows(tmp_path):
    data = make_xlsx(
        tmp_path / "b.xlsx",
        {"売上": [["月", "売上", "計画"], ["7月", 120, 150], ["8月", 95, 150]]},
    )
    result = XlsxExtractor().extract(data)

    assert list(result) == ["売上"]
    sheet = result["売上"]
    assert sheet["columns"] == ["月", "売上", "計画"]
    assert sheet["rows"][0] == {"月": "7月", "売上": 120, "計画": 150}
    assert sheet["row_count"] == 2
    assert sheet["truncated"] is False


def test_xlsx_extractor_keeps_each_sheet_separate(tmp_path):
    data = make_xlsx(
        tmp_path / "b.xlsx",
        {"売上": [["月"], ["7月"]], "原価": [["項目"], ["材料費"]]},
    )
    result = XlsxExtractor().extract(data)
    assert set(result) == {"売上", "原価"}
    assert result["原価"]["columns"] == ["項目"]


def test_xlsx_extractor_truncates_a_very_long_sheet(tmp_path):
    rows = [["n"]] + [[i] for i in range(50)]
    data = make_xlsx(tmp_path / "b.xlsx", {"Sheet1": rows})
    result = XlsxExtractor(max_rows=10).extract(data)
    assert result["Sheet1"]["truncated"] is True
    assert result["Sheet1"]["row_count"] == 10


def test_xlsx_extractor_rejects_something_that_is_not_a_workbook():
    with pytest.raises(ExtractionError, match="not a readable .xlsx"):
        XlsxExtractor().extract(b"not a workbook")


# --- docx --------------------------------------------------------------------


def test_docx_extractor_reads_paragraphs_and_tables(tmp_path):
    data = make_docx(
        tmp_path / "m.docx",
        ["契約Aは9月末で更新期限を迎える"],
        table=[["項目", "値"], ["期限", "9月30日"]],
    )
    text = DocxExtractor().extract(data)
    assert "契約Aは9月末で更新期限を迎える" in text
    assert "期限 | 9月30日" in text


def test_docx_extractor_rejects_something_that_is_not_a_document():
    with pytest.raises(ExtractionError, match="not a readable .docx"):
        DocxExtractor().extract(b"not a document")


# --- end to end: an authorized folder ----------------------------------------


async def test_office_files_in_a_watched_folder_become_knowledge(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_pptx(inbox / "deck.pptx", ["Q3の売上が計画を下回っている"])
    make_xlsx(inbox / "book.xlsx", {"売上": [["月", "売上"], ["7月", 120]]})
    make_docx(inbox / "memo.docx", ["契約Aは9月末で更新期限を迎える"])

    runtime = Runtime(tmp_path / "files.db")
    bootstrap_resources(runtime, scope=ResourceScope.for_root(inbox))
    bootstrap_observer(runtime)
    runtime.register_adapter(LocalFileAdapter(inbox, adapter_id="knowledge_local_file"))
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator, backend=None)

    await loop.start_file_observer(poll_interval=3600)
    await loop.reconcile()

    by_file = {
        item.metadata["resource_uri"].rsplit("/", 1)[-1]: item
        for item in loop.ledger.by_kind(KIND_RESOURCE_OBSERVATION)
    }
    assert set(by_file) == {"deck.pptx", "book.xlsx", "memo.docx"}
    assert "Q3の売上が計画を下回っている" in by_file["deck.pptx"].content.value
    assert "契約Aは9月末で更新期限を迎える" in by_file["memo.docx"].content.value
    assert by_file["book.xlsx"].content.value["売上"]["rows"][0]["売上"] == 120
    runtime.close()


async def test_editing_a_watched_deck_records_a_second_observation(tmp_path):
    """A changed file is new evidence, and the old reading stays on the record."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_pptx(inbox / "deck.pptx", ["当初の見通しは計画どおり"])

    clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = Runtime(tmp_path / "files.db", clock=clock)
    bootstrap_resources(runtime, scope=ResourceScope.for_root(inbox))
    bootstrap_observer(runtime)
    runtime.register_adapter(LocalFileAdapter(inbox, adapter_id="knowledge_local_file"))
    orchestrator = ProjectOrchestrator(
        runtime.db, agent_runtime=InProcessAgentRuntime()
    )
    loop = KnowledgeLoop(runtime, orchestrator, backend=None)
    await loop.start_file_observer(poll_interval=30)
    await loop.reconcile()

    make_pptx(inbox / "deck.pptx", ["計画を下回る見通しに変わった"])
    clock.advance(30)
    await runtime.tick()
    await loop.reconcile()

    readings = [
        item.content.value
        for item in loop.ledger.by_kind(KIND_RESOURCE_OBSERVATION)
        if item.metadata["resource_uri"].endswith("deck.pptx")
    ]
    assert len(readings) == 2
    assert any("当初の見通しは計画どおり" in text for text in readings)
    assert any("計画を下回る見通しに変わった" in text for text in readings)
    runtime.close()


# --- the standalone shortcut reads the same slides ---------------------------


def test_direct_ingest_reads_the_same_slides_as_the_pipeline(tmp_path):
    from nexus_seed.knowledge.ingest_pptx import extract_slide_texts

    path = tmp_path / "d.pptx"
    make_pptx(path, ["一枚目の内容", "二枚目の内容"])

    assert extract_slide_texts(path) == ["一枚目の内容", "二枚目の内容"]
