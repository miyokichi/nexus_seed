"""Clock abstraction so time-driven behaviour is testable.

Timers and retry backoff read the current time from a :class:`Clock`.  Tests
inject a :class:`ManualClock` to advance time instantly instead of sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..core.event import utcnow


class Clock:
    """Real wall-clock time (UTC)."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return utcnow()


class ManualClock(Clock):
    """A clock the test controls explicitly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or utcnow()

    def now(self) -> datetime:
        """Return the current (manually controlled) time."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now = self._now + timedelta(seconds=seconds)
