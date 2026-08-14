"""Persistence for :class:`~nexus_seed.delivery.models.EventDelivery`.

The UNIQUE constraint on ``event_id`` is what makes "every persisted event owes
exactly one routing attempt" a database fact rather than a convention.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..core.event import utcnow
from ..delivery.models import EventDelivery, EventDeliveryStatus
from .database import Database


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class EventDeliveryStore:
    """Tracks which persisted events still owe the system a routing attempt."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- creation ----------------------------------------------------------

    def create_for_event(
        self,
        event_id: uuid.UUID,
        *,
        status: EventDeliveryStatus = EventDeliveryStatus.PENDING,
    ) -> EventDelivery:
        """Record that ``event_id`` owes a routing attempt.

        Insert-or-ignore: appending the same event twice (which the event store
        itself already refuses) must not raise here.
        """
        delivery = EventDelivery(event_id=event_id, status=status)
        if status is EventDeliveryStatus.DELIVERED:
            delivery.delivered_at = delivery.created_at
        self.db.execute(
            """
            INSERT OR IGNORE INTO event_deliveries
                (id, event_id, status, attempt_count, next_attempt_at, last_error,
                 created_at, updated_at, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(delivery.id),
                str(event_id),
                delivery.status.value,
                delivery.attempt_count,
                None,
                None,
                delivery.created_at.isoformat(),
                delivery.updated_at.isoformat(),
                delivery.delivered_at.isoformat() if delivery.delivered_at else None,
            ),
        )
        return delivery

    # --- reads -------------------------------------------------------------

    def get(self, event_id: uuid.UUID) -> EventDelivery | None:
        """Return the delivery record for ``event_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM event_deliveries WHERE event_id = ?", (str(event_id),)
        )
        return self._row(row) if row else None

    def list_dispatchable(self, now: datetime, limit: int = 100) -> list[EventDelivery]:
        """Return deliveries that may be attempted right now, oldest event first.

        A RETRY_WAIT delivery whose backoff has not elapsed is *skipped*, not
        waited on — one poisoned event must never block the whole queue
        (spec §31).  Ordering is by the event store's insertion sequence, so
        dispatch is deterministic.

        The join is a LEFT join so an obligation whose event is somehow missing
        is still selected: the dispatcher can then settle it as FAILED instead
        of leaving it counted as outstanding forever.
        """
        rows = self.db.query(
            """
            SELECT d.* FROM event_deliveries d
            LEFT JOIN events e ON e.id = d.event_id
            WHERE d.status = ?
               OR (d.status = ? AND d.next_attempt_at IS NOT NULL
                   AND d.next_attempt_at <= ?)
            ORDER BY e.seq ASC
            LIMIT ?
            """,
            (
                EventDeliveryStatus.PENDING.value,
                EventDeliveryStatus.RETRY_WAIT.value,
                now.isoformat(),
                limit,
            ),
        )
        return [self._row(r) for r in rows]

    def by_status(self, status: EventDeliveryStatus | str) -> list[EventDelivery]:
        """Return every delivery in ``status``, oldest first."""
        value = status.value if isinstance(status, EventDeliveryStatus) else status
        rows = self.db.query(
            "SELECT * FROM event_deliveries WHERE status = ? ORDER BY created_at ASC",
            (value,),
        )
        return [self._row(r) for r in rows]

    def count_pending(self) -> int:
        """How many events still owe a routing attempt (any outstanding state)."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM event_deliveries WHERE status IN (?, ?, ?)",
            (
                EventDeliveryStatus.PENDING.value,
                EventDeliveryStatus.DELIVERING.value,
                EventDeliveryStatus.RETRY_WAIT.value,
            ),
        )
        return int(row["n"])

    def oldest_pending_at(self) -> datetime | None:
        """When the oldest outstanding delivery was created (operational health)."""
        row = self.db.query_one(
            "SELECT MIN(created_at) AS t FROM event_deliveries WHERE status IN (?, ?, ?)",
            (
                EventDeliveryStatus.PENDING.value,
                EventDeliveryStatus.DELIVERING.value,
                EventDeliveryStatus.RETRY_WAIT.value,
            ),
        )
        return _dt(row["t"]) if row and row["t"] else None

    def all(self) -> list[EventDelivery]:
        """Return every delivery record, oldest first."""
        rows = self.db.query("SELECT * FROM event_deliveries ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    # --- transitions -------------------------------------------------------

    def mark_delivering(self, event_id: uuid.UUID) -> None:
        """Claim a delivery before routing it.

        Committed on its own, so a crash during routing leaves a DELIVERING row
        that startup recovery recognises as interrupted.
        """
        self.db.execute(
            """
            UPDATE event_deliveries
            SET status = ?, attempt_count = attempt_count + 1, updated_at = ?
            WHERE event_id = ?
            """,
            (EventDeliveryStatus.DELIVERING.value, utcnow().isoformat(), str(event_id)),
        )

    def mark_delivered(self, event_id: uuid.UUID) -> None:
        """Record that routing committed.

        Must run inside the *same* transaction as the routing result, so an
        activation and its acknowledgement can never disagree (spec §15).
        """
        now = utcnow().isoformat()
        self.db.execute(
            """
            UPDATE event_deliveries
            SET status = ?, last_error = NULL, next_attempt_at = NULL,
                delivered_at = ?, updated_at = ?
            WHERE event_id = ?
            """,
            (EventDeliveryStatus.DELIVERED.value, now, now, str(event_id)),
        )

    def mark_retry(
        self, event_id: uuid.UUID, error: str, next_attempt_at: datetime
    ) -> None:
        """Schedule another attempt after a transient routing failure."""
        self.db.execute(
            """
            UPDATE event_deliveries
            SET status = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE event_id = ?
            """,
            (
                EventDeliveryStatus.RETRY_WAIT.value,
                error,
                next_attempt_at.isoformat(),
                utcnow().isoformat(),
                str(event_id),
            ),
        )

    def mark_failed(self, event_id: uuid.UUID, error: str) -> None:
        """Stop attempting an event.  Deliberately rare — see the model docstring."""
        self.db.execute(
            """
            UPDATE event_deliveries
            SET status = ?, last_error = ?, next_attempt_at = NULL, updated_at = ?
            WHERE event_id = ?
            """,
            (EventDeliveryStatus.FAILED.value, error, utcnow().isoformat(), str(event_id)),
        )

    def recover_stale_delivering(self) -> list[uuid.UUID]:
        """Return interrupted DELIVERING deliveries to PENDING.

        A committed routing always leaves DELIVERING in the same transaction,
        so anything still DELIVERING at startup died before committing and is
        safe to re-attempt — the same argument Phase 2A makes for RUNNING
        processes.
        """
        rows = self.db.query(
            "SELECT event_id FROM event_deliveries WHERE status = ?",
            (EventDeliveryStatus.DELIVERING.value,),
        )
        recovered = [uuid.UUID(r["event_id"]) for r in rows]
        if recovered:
            self.db.execute(
                "UPDATE event_deliveries SET status = ?, updated_at = ? WHERE status = ?",
                (
                    EventDeliveryStatus.PENDING.value,
                    utcnow().isoformat(),
                    EventDeliveryStatus.DELIVERING.value,
                ),
            )
        return recovered

    # --- migration ---------------------------------------------------------

    def backfill_missing(
        self, *, status: EventDeliveryStatus = EventDeliveryStatus.DELIVERED
    ) -> int:
        """Give delivery records to events that predate Phase 3F.

        Defaults to DELIVERED, not PENDING (spec §50).  Those events were
        already routed by the runtime that stored them; marking them pending
        would replay the entire history of a live database on first startup,
        re-running interpretations, work and actions.  Durable-delivery
        guarantees begin at the events Phase 3F itself persists.
        """
        rows = self.db.query(
            """
            SELECT e.id FROM events e
            LEFT JOIN event_deliveries d ON d.event_id = e.id
            WHERE d.event_id IS NULL
            ORDER BY e.seq ASC
            """
        )
        for row in rows:
            self.create_for_event(uuid.UUID(row["id"]), status=status)
        return len(rows)

    # --- row mapping -------------------------------------------------------

    @staticmethod
    def _row(row) -> EventDelivery:
        return EventDelivery(
            event_id=uuid.UUID(row["event_id"]),
            status=EventDeliveryStatus(row["status"]),
            attempt_count=row["attempt_count"],
            next_attempt_at=_dt(row["next_attempt_at"]),
            last_error=row["last_error"],
            delivered_at=_dt(row["delivered_at"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
