"""Adapter from the MVP Knowledge Gateway to the existing Knowledge Ledger."""

from __future__ import annotations

import json

from ..knowledge import KnowledgeLedger, KnowledgeRevision
from .models import JsonObject, JsonValue, KnowledgeItem


class ExistingKnowledgeGateway:
    """Expose :class:`KnowledgeLedger` through the small MVP gateway.

    The ledger remains append-only.  Putting an existing stable id with new
    content creates a revision; putting exactly the same value is idempotent.
    """

    def __init__(self, ledger: KnowledgeLedger) -> None:
        self.ledger = ledger

    def put(self, item: KnowledgeItem) -> None:
        """Record one normalized MVP Knowledge item in the existing ledger."""

        metadata = dict(item.metadata)
        current = self.ledger.head(item.id)
        if current is None:
            self.ledger.record(
                item.content,
                source_type="mvp",
                source_ref=item.source,
                knowledge_id=item.id,
                recorded_at=item.created_at,
                format=_content_format(item.content),
                metadata=metadata,
            )
        elif (
            current.content.value == item.content
            and current.source.ref == item.source
            and _public_metadata(current.metadata) == metadata
        ):
            return
        else:
            self.ledger.revise(
                item.id,
                value=item.content,
                source_type="mvp",
                source_ref=item.source,
                format=_content_format(item.content),
                recorded_at=item.created_at,
                metadata=metadata,
                reason="MVP KnowledgeGateway.put",
            )

    def get(self, item_id: str) -> KnowledgeItem | None:
        """Return the current ledger revision for ``item_id``."""

        revision = self.ledger.head(item_id)
        return _from_revision(revision) if revision else None

    def search(self, query: str) -> list[KnowledgeItem]:
        """Case-insensitively search current content, source and metadata.

        This is deliberately lexical and bounded by the existing ledger.  It
        does not introduce a vector database or semantic retrieval into MVP.
        """

        needle = query.casefold().strip()
        matches = []
        for revision in self.ledger.all_heads():
            item = _from_revision(revision)
            searchable = json.dumps(
                {
                    "content": item.content,
                    "source": item.source,
                    "metadata": item.metadata,
                },
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            ).casefold()
            if not needle or needle in searchable:
                matches.append(item)
        return sorted(matches, key=lambda item: (item.created_at, item.id))


def _from_revision(revision: KnowledgeRevision) -> KnowledgeItem:
    metadata = _public_metadata(revision.metadata)
    return KnowledgeItem(
        id=revision.knowledge_id,
        content=revision.content.value,
        source=revision.source.ref or revision.source.type,
        created_at=revision.recorded_at,
        metadata=metadata,
    )


def _content_format(content: JsonValue) -> str:
    return "text" if isinstance(content, str) else "json"


def _public_metadata(metadata: JsonObject) -> JsonObject:
    result = dict(metadata)
    result.pop("revision_reason", None)
    result.pop("mvp_model", None)  # compatibility with pre-freeze MVP rows
    return result


__all__ = ["ExistingKnowledgeGateway"]
