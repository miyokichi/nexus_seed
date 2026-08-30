from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "read_excel":
            result = read_excel(workspace, arguments)
        elif tool == "write_excel":
            result = write_excel(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown Excel tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Excel script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def read_excel(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = safe_path(workspace, str(arguments["path"]), ".xlsx")
    sheet_filter = str(arguments.get("sheet") or "").strip()
    max_rows = int(arguments.get("max_rows") or 100)
    if not path.exists():
        return {"ok": False, "content": f"File not found: {path.relative_to(workspace)}"}

    with zipfile.ZipFile(path) as book:
        shared_strings = read_shared_strings(book)
        sheets = workbook_sheets(book)
        sections = []
        for sheet_name, sheet_path in sheets:
            if sheet_filter and sheet_filter != sheet_name:
                continue
            rows = read_sheet(book, sheet_path, shared_strings)
            sections.append(format_sheet(sheet_name, rows[:max_rows]))

    if not sections:
        return {"ok": False, "content": f"Sheet not found: {sheet_filter}" if sheet_filter else "No sheets found."}
    return {"ok": True, "content": "\n\n".join(sections)}


def write_excel(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = safe_path(workspace, str(arguments["path"]), ".xlsx")
    sheet_name = clean_sheet_name(str(arguments.get("sheet_name") or "Sheet1"))
    rows = normalize_rows(arguments)
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", content_types())
        book.writestr("_rels/.rels", root_rels())
        book.writestr("xl/workbook.xml", workbook_xml(sheet_name))
        book.writestr("xl/_rels/workbook.xml.rels", workbook_rels())
        book.writestr("xl/styles.xml", styles_xml())
        book.writestr("xl/worksheets/sheet1.xml", sheet_xml(rows))

    return {"ok": True, "content": f"Wrote {path.relative_to(workspace)} ({len(rows)} rows)"}


def read_shared_strings(book: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("a:si", NS):
        strings.append("".join(node.text or "" for node in item.findall(".//a:t", NS)))
    return strings


def workbook_sheets(book: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        rel.attrib["Id"]: "xl/" + rel.attrib["Target"].lstrip("/")
        for rel in rels.findall("rel:Relationship", NS)
    }
    sheets = []
    for sheet in workbook.findall(".//a:sheet", NS):
        name = sheet.attrib.get("name", "Sheet")
        rid = sheet.attrib.get(f"{{{NS['r']}}}id", "")
        if rid in rel_map:
            sheets.append((name, rel_map[rid]))
    return sheets


def read_sheet(book: zipfile.ZipFile, sheet_path: str, shared_strings: list[str]) -> list[list[str]]:
    root = ET.fromstring(book.read(sheet_path))
    rows: list[list[str]] = []
    for row in root.findall(".//a:row", NS):
        values: list[str] = []
        for cell in row.findall("a:c", NS):
            ref = cell.attrib.get("r", "")
            col = column_index(ref)
            while len(values) < col:
                values.append("")
            values.append(cell_text(cell, shared_strings))
        rows.append(values)
    return rows


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//a:t", NS))
    value = cell.find("a:v", NS)
    raw = value.text if value is not None else ""
    if cell_type == "s" and raw.isdigit():
        index = int(raw)
        return shared_strings[index] if index < len(shared_strings) else ""
    return raw or ""


def format_sheet(name: str, rows: list[list[str]]) -> str:
    lines = [f"# Sheet: {name}"]
    lines.extend("\t".join(row).rstrip() for row in rows)
    return "\n".join(lines)


def normalize_rows(arguments: dict[str, Any]) -> list[list[Any]]:
    rows = arguments.get("rows")
    if isinstance(rows, list):
        return [row if isinstance(row, list) else [row] for row in rows]
    csv_text = str(arguments.get("csv_text") or "")
    if csv_text.strip():
        return list(csv.reader(io.StringIO(csv_text)))
    return []


def sheet_xml(rows: list[list[Any]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{column_name(col_index)}{row_index}"
            cells.append(cell_xml(ref, value))
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )


def cell_xml(ref: str, value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape_xml(str(value))}</t></is></c>'


def content_types() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""


def root_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def workbook_xml(sheet_name: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{escape_xml(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""


def workbook_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""


def safe_path(workspace: Path, requested: str, suffix: str) -> Path:
    path = Path(requested)
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    if workspace not in [resolved, *resolved.parents]:
        raise ValueError("Path escaped the workspace.")
    if resolved.suffix.lower() != suffix:
        raise ValueError(f"Expected a {suffix} file.")
    return resolved


def clean_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", name).strip() or "Sheet1"
    return cleaned[:31]


def column_index(ref: str) -> int:
    letters = "".join(char for char in ref if char.isalpha())
    value = 0
    for char in letters:
        value = value * 26 + ord(char.upper()) - 64
    return max(value - 1, 0)


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


if __name__ == "__main__":
    raise SystemExit(main())
