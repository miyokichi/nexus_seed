"""DurableEventDispatcher — makes sure every stored event reaches the Router.

Pure mechanism.  It reads outstanding delivery records, hands each event to the
*existing* Router, and acknowledges the obligation.  It knows nothing about
what events mean, which processes matter, or what work should follow — that all
stays where it already lived.

The one subtle rule is where the acknowledgement is committed (spec §14–§15).
Marking an event delivered because ``route()`` returned would be a lie: the
routing result and the acknowledgement could then disagree across a crash, and
a re-dispatch would start the same processes again.  So routing and
acknowledgement commit in **one transaction**:

    mark DELIVERING (own commit)   -> a crash here is visible and recoverable
    [ route + mark DELIVERED ]     -> one transaction; both or neither

Delivery is at-least-once.  Exactly-once *outcomes* come from the idempotency
each layer already has — activation ledger, work_key, action key, content hash
(spec §24–§25).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..core.event import Event
from .models import EventDelivery, EventDeliveryStatus, backoff_seconds

logger = logging.getLogger("nexus_seed.delivery")

#: How many deliveries one sweep will attempt.
DEFAULT_BATCH = 100


class DurableEventDispatcher:
    """Routes persisted-but-unacknowledged events, with retry and recovery."""

    def __init__(
        self,
        db,
        event_store,
        delivery_store,
        router,
        clock,
        *,
        batch_size: int = DEFAULT_BATCH,
        max_attempts: int | None = None,
    ) -> None:
        self.db = db
        self.event_store = event_store
        self.deliveries = delivery_store
        self.router = router
        self.clock = clock
        self.batch_size = batch_size
        #: ``None`` means keep retrying forever.  Phase 3F prefers an event
        #: that keeps failing loudly to one that is quietly abandoned.
        self.max_attempts = max_attempts

    # --- sweeping ----------------------------------------------------------

    def dispatch_pending(self, limit: int | None = None) -> int:
        """Attempt every dispatchable delivery once; return how many succeeded.

        Deliveries still inside their retry backoff are skipped rather than
        waited for, so one failing event cannot stall the queue.
        """
        now = self.clock.now()
        delivered = 0
        for delivery in self.deliveries.list_dispatchable(
            now, limit if limit is not None else self.batch_size
        ):
            if self.dispatch(delivery):
                delivered += 1
        return delivered

    def dispatch(self, delivery: EventDelivery) -> bool:
        """Attempt one delivery.  Returns whether it was acknowledged."""
        event = self.event_store.get(delivery.event_id)
        if event is None:
            # An acknowledgement we can never fulfil: the event is gone.  Fail
            # it rather than retry forever against nothing.
            logger.error("delivery %s references missing event", delivery.event_id)
            self.deliveries.mark_failed(delivery.event_id, "event not found")
            return False

        # Claimed in its own commit, so an interrupted attempt is visible.
        self.deliveries.mark_delivering(delivery.event_id)

        try:
            with self.db.atomic():
                activated = self.router.route(event)
                self.deliveries.mark_delivered(event.id)
        except Exception as exc:  # noqa: BLE001 - a bad route must not stop the sweep
            logger.exception("routing event %s failed", event.id)
            self._schedule_retry(delivery, exc)
            return False

        logger.info(
            "event %s (%s) delivered -> %d activation(s)",
            event.id,
            event.type,
            len(activated),
        )
        return True

    def _schedule_retry(self, delivery: EventDelivery, error: Exception) -> None:
        attempt = delivery.attempt_count + 1
        if self.max_attempts is not None and attempt >= self.max_attempts:
            self.deliveries.mark_failed(delivery.event_id, str(error))
            logger.error(
                "event %s delivery FAILED after %d attempts", delivery.event_id, attempt
            )
            return
        delay = backoff_seconds(attempt)
        self.deliveries.mark_retry(
            delivery.event_id,
            str(error),
            self.clock.now() + timedelta(seconds=delay),
        )
        logger.warning(
            "event %s delivery attempt %d failed (%s); retrying in %.0fs",
            delivery.event_id,
            attempt,
            error,
            delay,
        )

    # --- recovery ----------------------------------------------------------

    def recover(self) -> list:
        """Return interrupted DELIVERING deliveries to PENDING.

        Called at startup.  A committed routing always sets DELIVERED in the
        same transaction, so anything still DELIVERING died before committing
        and is safe to re-attempt.
        """
        recovered = self.deliveries.recover_stale_delivering()
        for event_id in recovered:
            logger.warning("recovered stale delivery for event %s", event_id)
        return recovered

    def backfill_legacy(self) -> int:
        """Give pre-Phase-3F events a delivery record (as already DELIVERED)."""
        count = self.deliveries.backfill_missing(
            status=EventDeliveryStatus.DELIVERED
        )
        if count:
            logger.info(
                "backfilled %d legacy event(s) as DELIVERED (pre-3F history)", count
            )
        return count

    # --- observability -----------------------------------------------------

    def pending_count(self) -> int:
        """How many events still owe a routing attempt."""
        return self.deliveries.count_pending()

    def health(self) -> dict:
        """A small operational snapshot of the delivery queue."""
        oldest = self.deliveries.oldest_pending_at()
        now = self.clock.now()
        return {
            "pending": len(self.deliveries.by_status(EventDeliveryStatus.PENDING)),
            "delivering": len(self.deliveries.by_status(EventDeliveryStatus.DELIVERING)),
            "retry_wait": len(self.deliveries.by_status(EventDeliveryStatus.RETRY_WAIT)),
            "failed": len(self.deliveries.by_status(EventDeliveryStatus.FAILED)),
            "outstanding": self.deliveries.count_pending(),
            "oldest_pending_age_seconds": (
                (now - oldest).total_seconds() if oldest is not None else None
            ),
        }


def event_summary(event: Event) -> str:  # pragma: no cover - logging helper
    """A short description of an event, for delivery logs."""
    return f"{event.type}({event.id})"


def dispatchable_at(delivery: EventDelivery, now: datetime) -> bool:
    """Whether ``delivery`` may be attempted at ``now``."""
    if delivery.status is EventDeliveryStatus.PENDING:
        return True
    if delivery.status is EventDeliveryStatus.RETRY_WAIT:
        return delivery.next_attempt_at is not None and delivery.next_attempt_at <= now
    return False
