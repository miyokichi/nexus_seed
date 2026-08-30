"""Bootstrap Context — the three files a person writes NEXUS SEED's world in.

    NEXUS_SEED_DATA_DIR/context/
      terms.md       what the words mean here
      goals.md       what a good state looks like, and what constrains it
      situation.md   what is true right now

They are read as **prose and kept as prose**.  Nothing here parses them into a
schema, and nothing downstream is allowed to replace the text with its reading
of the text: an interpretation is a later, separate, optional act, exactly as
everywhere else in the Knowledge Runtime.  That is why a document is a
Knowledge object like any other and its revisions accumulate as history — a
sentence a person deletes from ``goals.md`` is still what they believed on the
day they wrote it, and a later Principle Extraction is entitled to see that.

Syncing is content-addressed: an unchanged file writes nothing, so a tick that
touches nothing costs one read per file and no Ledger rows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .ledger import KnowledgeLedger
from .models import KnowledgeRevision

logger = logging.getLogger("nexus_seed.knowledge.bootstrap_context")

#: Directory under the data root holding the bootstrap context.
CONTEXT_DIR = "context"

#: The Knowledge ``kind`` every bootstrap document is recorded under.
KIND_CONTEXT_DOCUMENT = "context_document"

#: Filename -> the role it plays, in the order a reader should meet them:
#: what words mean, then what is wanted, then what is.
CONTEXT_DOCUMENTS: dict[str, str] = {
    "terms.md": "terms",
    "goals.md": "goals",
    "situation.md": "situation",
}

_TEMPLATES: dict[str, str] = {
    "terms.md": """# 基本用語

ここで使う言葉の意味・略語を、普通の文章で書いてください。
NEXUS SEEDはこの原文をそのまま保持します。決まった書式はありません。

例:
- BLOCKED: Projectが自力で先に進めない状態。
""",
    "goals.md": """# Goal

望ましい状態・方針・制約を、普通の文章で書いてください。
数値目標へ変換する必要はありません。

例:
- BLOCKED Projectを放置しない。
""",
    "situation.md": """# 現在の状況

いま何が起きているかを、普通の文章で書いてください。

例:
- Project Aはデータ不足でBLOCKED。
""",
}


def context_id(name: str) -> str:
    """The stable Knowledge id of one bootstrap document."""
    return f"context:{CONTEXT_DOCUMENTS.get(name, name)}"


class ContextDocuments:
    """Reads ``context/`` and keeps the Ledger's copy of it current."""

    def __init__(self, ledger: KnowledgeLedger, root: str | Path) -> None:
        self.ledger = ledger
        #: The ``context/`` directory itself, not the data root.
        self.root = Path(root).expanduser()

    # --- files --------------------------------------------------------------

    def ensure(self) -> list[Path]:
        """Create the directory and any missing document; return their paths.

        A missing file is created with a short prompt rather than left absent,
        because an empty ``goals.md`` a person can open and type into is a far
        better invitation than an error saying one was expected.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, template in _TEMPLATES.items():
            path = self.root / name
            if not path.exists():
                path.write_text(template, encoding="utf-8")
                logger.info("created bootstrap context document %s", path)
            paths.append(path)
        return paths

    def read(self, name: str) -> str | None:
        """Return one document's text, or ``None`` if it is not there."""
        path = self.root / name
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("could not read %s: %s", path, exc)
            return None

    # --- ledger -------------------------------------------------------------

    def sync(self) -> list[KnowledgeRevision]:
        """Record every document whose text differs from what is on record.

        Returns only what actually changed, so a caller can tell "the context
        moved" from "the context is as it was" without diffing anything.
        """
        changed = []
        for name, role in CONTEXT_DOCUMENTS.items():
            text = self.read(name)
            if text is None:
                continue
            knowledge_id = context_id(name)
            current = self.ledger.head(knowledge_id)
            if current is not None and current.content.value == text:
                continue
            metadata = {"document": name, "role": role, "path": str(self.root / name)}
            if current is None:
                changed.append(
                    self.ledger.record(
                        text,
                        knowledge_id=knowledge_id,
                        source_type="bootstrap_context",
                        source_ref=name,
                        format="markdown",
                        kind=KIND_CONTEXT_DOCUMENT,
                        metadata=metadata,
                    )
                )
            else:
                changed.append(
                    self.ledger.revise(
                        knowledge_id,
                        value=text,
                        metadata=metadata,
                        reason=f"{name} changed on disk",
                    )
                )
            logger.info("bootstrap context %s is now revision %d", name, changed[-1].revision)
        return changed

    def documents(self) -> dict[str, KnowledgeRevision]:
        """Return the current revision of each document, keyed by role."""
        found = {}
        for name, role in CONTEXT_DOCUMENTS.items():
            head = self.ledger.head(context_id(name))
            if head is not None:
                found[role] = head
        return found

    def as_context(self) -> dict[str, dict[str, object]]:
        """Return the documents in the shape an assessment prompt wants."""
        return {
            role: {
                "knowledge_id": head.knowledge_id,
                "revision": head.revision,
                "text": head.content.value,
            }
            for role, head in self.documents().items()
        }

    def revision_key(self) -> str:
        """A stable key for "the context as it currently stands".

        Two syncs of unchanged files produce the same key, which is what lets
        an assessment be skipped without asking an LLM whether anything moved.
        """
        return "|".join(
            f"{role}:{head.id}" for role, head in sorted(self.documents().items())
        )


__all__ = [
    "CONTEXT_DIR",
    "CONTEXT_DOCUMENTS",
    "KIND_CONTEXT_DOCUMENT",
    "ContextDocuments",
    "context_id",
]
