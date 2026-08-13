"""Persistence for :class:`~nexus_seed.ingress.models.IngressReceipt`.

The UNIQUE index on ``(adapter_id, source_event_key)`` is the whole mechanism
behind Invariant 31: the database, not application logic, is what guarantees a
redelivered external occurrence cannot become a second Event.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from ..ingress.models import DuplicateIngress, IngressReceipt, IngressStatus
from .database import Database, dumps, loads

__all__ = ["DuplicateIngress", "IngressReceiptStore"]


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class IngressReceiptStore:
    """Stores one receipt per logical external event."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, receipt: IngressReceipt) -> IngressReceipt:
        """Insert a receipt.

        Raises:
            DuplicateIngress: If ``(adapter_id, source_event_key)`` is taken.
                Deliberately *not* an upsert: silently overwriting would erase
                the record of the first delivery and hide the duplication.
        """
        try:
            self.db.execute(
                """
                INSERT INTO ingress_receipts
                    (id, adapter_id, source_type, source_event_key, event_type,
                     payload_json, source_cursor, metadata_json, reasons_json,
                     observed_at, received_at, event_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(receipt.id),
                    receipt.adapter_id,
                    receipt.source_type,
                    receipt.source_event_key,
                    receipt.event_type,
                    dumps(receipt.payload),
                    receipt.source_cursor,
                    dumps(receipt.metadata),
                    dumps(list(receipt.reasons)),
                    receipt.observed_at.isoformat(),
                    receipt.received_at.isoformat(),
                    str(receipt.event_id) if receipt.event_id else None,
                    receipt.status.value,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIngress(
                f"{receipt.adapter_id}/{receipt.source_event_key} already ingested"
            ) from exc
        return receipt

    def get(self, receipt_id: uuid.UUID) -> IngressReceipt | None:
        """Return the receipt with ``receipt_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM ingress_receipts WHERE id = ?", (str(receipt_id),)
        )
        return self._row(row) if row else None

    def get_by_source_key(
        self, adapter_id: str, source_event_key: str
    ) -> IngressReceipt | None:
        """Return the receipt for one external identity, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM ingress_receipts WHERE adapter_id = ? AND source_event_key = ?",
            (adapter_id, source_event_key),
        )
        return self._row(row) if row else None

    def for_event(self, event_id: uuid.UUID) -> IngressReceipt | None:
        """Return the receipt that produced ``event_id``, or ``None``.

        ``None`` means the event originated inside NEXUS SEED.
        """
        row = self.db.query_one(
            "SELECT * FROM ingress_receipts WHERE event_id = ?", (str(event_id),)
        )
        return self._row(row) if row else None

    def for_adapter(self, adapter_id: str) -> list[IngressReceipt]:
        """Return every receipt from ``adapter_id``, oldest first."""
        rows = self.db.query(
            "SELECT * FROM ingress_receipts WHERE adapter_id = ? ORDER BY received_at ASC",
            (adapter_id,),
        )
        return [self._row(r) for r in rows]

    def all(self) -> list[IngressReceipt]:
        """Return every receipt, oldest first."""
        rows = self.db.query("SELECT * FROM ingress_receipts ORDER BY received_at ASC")
        return [self._row(r) for r in rows]

    def by_status(self, status: IngressStatus | str) -> list[IngressReceipt]:
        """Return receipts in ``status``, oldest first."""
        value = status.value if isinstance(status, IngressStatus) else status
        rows = self.db.query(
            "SELECT * FROM ingress_receipts WHERE status = ? ORDER BY received_at ASC",
            (value,),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> IngressReceipt:
        return IngressReceipt(
            adapter_id=row["adapter_id"],
            source_type=row["source_type"],
            source_event_key=row["source_event_key"],
            event_type=row["event_type"],
            payload=loads(row["payload_json"]) or {},
            source_cursor=row["source_cursor"],
            metadata=loads(row["metadata_json"]) or {},
            observed_at=datetime.fromisoformat(row["observed_at"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            event_id=_uuid(row["event_id"]),
            status=IngressStatus(row["status"]),
            reasons=loads(row["reasons_json"]) or [],
            id=uuid.UUID(row["id"]),
        )
