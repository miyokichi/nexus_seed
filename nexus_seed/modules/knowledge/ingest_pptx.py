"""Ingest local ``.pptx`` files into the Knowledge Ledger, one slide each.

Deliberately the simple first step, not the final integration: each slide's
text becomes one ``kind=raw`` Knowledge object, related (``about``) to its
deck, so K3's ``select_candidates(ledger, about=...)`` groups a deck's slides
for consolidation without any change to ``nexus_seed/knowledge/consolidation.py``.
A proper ``Extractor`` registered in ``nexus_seed/resources/extractors.py``
— reused by the existing Resource/ingress pipeline, with versioning and
deduplication for free — is the natural next step and is not what this does.

Usage::

    python -m nexus_seed.knowledge.ingest_pptx --db nexus_knowledge.db slide.pptx deck2.pptx
    python -m nexus_seed.knowledge.ingest_pptx --db nexus_knowledge.db --dir ./docs --about project-A

Requires ``python-pptx`` (``pip install python-pptx``), imported lazily so it
is never a hard dependency of ``nexus_seed`` itself (AGENTS.md: keep runtime
dependencies minimal).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ...resources.extractors import slide_texts
from ...storage.database import Database
from .adapters.sqlite import KnowledgeStore
from .ledger import KnowledgeLedger
from .models import RELATION_ABOUT, KnowledgeRevision, Relation


def _pptx_module():
    try:
        import pptx
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "python-pptx is required for pptx ingestion. Install it with:\n"
            "    pip install python-pptx"
        ) from exc
    return pptx


def extract_slide_texts(path: Path) -> list[str]:
    """Return one text block per slide (every text-bearing shape, in order).

    Reading is delegated to the Resource pipeline's :func:`slide_texts`, so a
    deck read through this shortcut and one read through the file observer see
    exactly the same slides.  The per-slide ``[slide N]`` marker that the
    Resource representation carries is dropped here, because this path records
    one Knowledge object per slide and the position is already in its
    ``source_ref``.
    """
    presentation = _pptx_module().Presentation(str(path))
    return [
        block.split("\n", 1)[1] if block.startswith("[slide ") else block
        for block in slide_texts(presentation)
    ]


def ingest_pptx_file(
    ledger: KnowledgeLedger, path: Path, *, about: str | None = None
) -> list[KnowledgeRevision]:
    """Record each non-empty slide of one ``.pptx`` file as raw Knowledge.

    Every slide carries an ``about`` relation to the deck (its filename stem
    by default, or ``about`` if given — pass the same value across several
    files to group them for one later consolidation).
    """
    subject = about or path.stem
    created = []
    for index, text in enumerate(extract_slide_texts(path), start=1):
        if not text:
            continue
        rev = ledger.record(text, source_type="pptx", source_ref=f"{path.name}#slide{index}")
        rev = ledger.relate(rev.knowledge_id, Relation(type=RELATION_ABOUT, target=subject))
        created.append(rev)
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-seed-knowledge-ingest-pptx",
        description="Record each slide of one or more local .pptx files as raw Knowledge.",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument(
        "--about",
        default=None,
        help="relation target shared by every slide of every file given "
        "(default: each file's own name, so files are grouped separately)",
    )
    parser.add_argument("files", nargs="*", help=".pptx file paths")
    parser.add_argument(
        "--dir", default=None, help="also ingest every .pptx found under this directory (recursive)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = [Path(f) for f in args.files]
    if args.dir:
        paths.extend(sorted(Path(args.dir).rglob("*.pptx")))
    if not paths:
        print("no .pptx files given (pass file paths and/or --dir)", file=sys.stderr)
        return 2

    db = Database(args.db)
    ledger = KnowledgeLedger(KnowledgeStore(db))
    total = 0
    for path in paths:
        if not path.exists():
            print(f"skip (not found): {path}", file=sys.stderr)
            continue
        created = ingest_pptx_file(ledger, path, about=args.about)
        ids = ", ".join(rev.knowledge_id for rev in created) or "-"
        print(f"{path}: {len(created)} slide(s) -> Knowledge ({ids})")
        total += len(created)
    print(f"total: {total} Knowledge object(s) recorded into {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
