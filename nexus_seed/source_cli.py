"""Convert one business document into Canonical YAML v0.1 from the shell.

    nexus-seed-source excel assumption.xlsx \
        --mapping samples/excel_assumption_mapping.yaml --out assumption.yaml

The result is the input of ``nexus-seed-semantica ingest``: converting and
ingesting are separate steps on purpose, so the Canonical YAML can be read,
reviewed and corrected by a person before it becomes Knowledge.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .sources.excel import ExcelSourceError, convert_excel_to_yaml


def build_parser() -> argparse.ArgumentParser:
    """Build the source-document converter parser."""

    parser = argparse.ArgumentParser(
        prog="nexus-seed-source",
        description="Convert a source document into Canonical YAML v0.1.",
    )
    subcommands = parser.add_subparsers(dest="format", required=True)
    excel = subcommands.add_parser("excel", help="convert one .xlsx sheet")
    excel.add_argument("workbook", help="path to the .xlsx file")
    excel.add_argument(
        "--mapping",
        required=True,
        help="column-to-Canonical mapping YAML (see samples/)",
    )
    excel.add_argument(
        "--document-id",
        default=None,
        help="override the document id declared in the mapping",
    )
    excel.add_argument(
        "--out",
        default=None,
        help="write the Canonical YAML here instead of stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Convert one document and return a process exit code."""

    args = build_parser().parse_args(argv)
    try:
        canonical = convert_excel_to_yaml(
            Path(args.workbook).expanduser(),
            Path(args.mapping).expanduser(),
            document_id=args.document_id,
        )
    except ExcelSourceError as exc:
        print(f"conversion error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        destination = Path(args.out).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(canonical, encoding="utf-8")
        print(str(destination))
    else:
        print(canonical, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
