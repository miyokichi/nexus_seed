"""Regenerate ``tests/fixtures/assumption.xlsx``.

The workbook is committed so the E2E reads a real ``.xlsx`` written by Excel's
own format, and this script is committed so the binary stays reviewable::

    python tests/fixtures/make_assumption_xlsx.py
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

HEADERS = [
    "Generation ID",
    "Generation",
    "Parameter ID",
    "Parameter",
    "Parameter Aliases",
    "Typical",
    "Variation",
    "Unit",
    "Deliverable ID",
    "Deliverable",
    "Required",
    "Status",
]

ROWS = [
    [
        "gen_x",
        "GenX",
        "wl_width",
        "WL Width",
        "WL width",
        50,
        10,
        "nm",
        "performance_report",
        "Performance Evaluation Report",
        True,
        "missing",
    ]
]


def main() -> None:
    """Write the one-sheet assumption workbook next to this script."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "GenX"
    sheet.append(HEADERS)
    for row in ROWS:
        sheet.append(row)
    workbook.save(Path(__file__).with_name("assumption.xlsx"))


if __name__ == "__main__":  # pragma: no cover - developer utility
    main()
