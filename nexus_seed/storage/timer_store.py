"""Persistence for timers — the basis for time-driven ("timer") events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from ..core.event import utcnow
from .database import Database, dumps, loads


@dataclass
class TimerRecord:
    """A scheduled timer that fires a ``timer_fired`` event at ``fire_at``."""

    fire_at: datetime
    event_type: str = "timer_fired"
    payload: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    fired: bool = False
    created_at: datetime = field(default_factory=utcnow)


class TimerStore:
    """Stores timers and finds those that are due."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, timer: TimerRecord) -> TimerRecord:
        """Insert or update a timer."""
        self.db.execute(
            """
            INSERT INTO timers (id, fire_at, event_type, payload, fired, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fire_at = excluded.fire_at,
                event_type = excluded.event_type,
                payload = excluded.payload,
                fired = excluded.fired
            """,
            (
                str(timer.id),
                timer.fire_at.isoformat(),
                timer.event_type,
                dumps(timer.payload),
                int(timer.fired),
                timer.created_at.isoformat(),
            ),
        )
        return timer

    def due(self, now: datetime) -> list[TimerRecord]:
        """Return unfired timers whose ``fire_at`` is at or before ``now``."""
        rows = self.db.query(
            "SELECT * FROM timers WHERE fired = 0 AND fire_at <= ? ORDER BY fire_at ASC",
            (now.isoformat(),),
        )
        return [self._row(r) for r in rows]

    def mark_fired(self, timer_id: uuid.UUID) -> None:
        """Mark a timer as fired so it is not delivered again."""
        self.db.execute("UPDATE timers SET fired = 1 WHERE id = ?", (str(timer_id),))

    def all(self) -> list[TimerRecord]:
        """Return every timer in creation order."""
        rows = self.db.query("SELECT * FROM timers ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> TimerRecord:
        return TimerRecord(
            fire_at=datetime.fromisoformat(row["fire_at"]),
            event_type=row["event_type"],
            payload=loads(row["payload"]) or {},
            id=uuid.UUID(row["id"]),
            fired=bool(row["fired"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
