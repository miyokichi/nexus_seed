"""Persistence for :class:`~nexus_seed.world.observation.Observation`."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..world.observation import Observation
from .database import Database, dumps, loads


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


class ObservationStore:
    """Stores observations so the reasoning history stays traceable."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, observation: Observation) -> Observation:
        """Persist an observation (insert; observations are not mutated)."""
        self.db.execute(
            """
            INSERT INTO observations
                (id, source_event_id, created_by_process_id, subject, predicate,
                 extracted, confidence, proposal_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(observation.id),
                str(observation.source_event_id) if observation.source_event_id else None,
                str(observation.created_by_process_id)
                if observation.created_by_process_id
                else None,
                observation.subject,
                observation.predicate,
                dumps(observation.extracted),
                observation.confidence,
                str(observation.proposal_id) if observation.proposal_id else None,
                observation.created_at.isoformat(),
            ),
        )
        return observation

    def get(self, observation_id: uuid.UUID) -> Observation | None:
        """Return the observation with ``observation_id``, or ``None``."""
        row = self.db.query_one(
            "SELECT * FROM observations WHERE id = ?", (str(observation_id),)
        )
        return self._row(row) if row else None

    def for_event(self, source_event_id: uuid.UUID) -> list[Observation]:
        """Return all observations derived from ``source_event_id``."""
        rows = self.db.query(
            "SELECT * FROM observations WHERE source_event_id = ? ORDER BY created_at ASC",
            (str(source_event_id),),
        )
        return [self._row(r) for r in rows]

    def all(self) -> list[Observation]:
        """Return all observations in creation order."""
        rows = self.db.query("SELECT * FROM observations ORDER BY created_at ASC")
        return [self._row(r) for r in rows]

    def recent(self, n: int) -> list[Observation]:
        """Return the ``n`` most recent observations, oldest first."""
        rows = self.db.query(
            "SELECT * FROM observations ORDER BY created_at DESC LIMIT ?", (n,)
        )
        return [self._row(r) for r in reversed(rows)]

    def by_subject(self, subject: str) -> list[Observation]:
        """Return all observations about ``subject`` in creation order."""
        rows = self.db.query(
            "SELECT * FROM observations WHERE subject = ? ORDER BY created_at ASC",
            (subject,),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Observation:
        return Observation(
            subject=row["subject"],
            predicate=row["predicate"],
            extracted=loads(row["extracted"]) or {},
            source_event_id=_uuid(row["source_event_id"]),
            created_by_process_id=_uuid(row["created_by_process_id"]),
            confidence=row["confidence"],
            proposal_id=_uuid(row["proposal_id"]),
            id=uuid.UUID(row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
