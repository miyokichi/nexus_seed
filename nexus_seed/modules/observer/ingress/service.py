"""IngressService — the single door through which the world gets in.

Everything external passes here (Invariant 28), and this is the only place that
turns an :class:`IngressEnvelope` into an ``Event``.  Its five jobs, in order::

    validate -> deduplicate -> persist (receipt + event + checkpoint) -> deliver

It holds **no source-specific knowledge**.  Deciding what makes two webhook
deliveries "the same delivery", or two file scans "the same file version", is
the adapter's job (spec §17) — the service only trusts the resulting
``source_event_key``.

Atomicity (spec §15): the receipt, the Event and the checkpoint advance commit
in one SQLite transaction.  Delivery into the Runtime happens *after* that
commit, so a failure while processing leaves a durable, already-deduplicated
event rather than a half-ingested one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ....core.event import Event
from .models import (
    AdapterCheckpoint,
    DuplicateIngress,
    IngressEnvelope,
    IngressReceipt,
    IngressResult,
    IngressStatus,
)
from .validation import is_keyable, validate_envelope

if TYPE_CHECKING:  # pragma: no cover - typing only; storage imports ingress
    from ..adapters.checkpoint_sqlite import AdapterCheckpointStore
    from ..adapters.ingress_sqlite import IngressReceiptStore

logger = logging.getLogger("nexus_seed.ingress")


class IngressService:
    """Converts validated, deduplicated external occurrences into Events."""

    def __init__(
        self,
        runtime,
        *,
        receipt_store: "IngressReceiptStore | None" = None,
        checkpoint_store: "AdapterCheckpointStore | None" = None,
    ) -> None:
        self.runtime = runtime
        self.db = runtime.db
        self.event_store = runtime.event_store
        self.receipts = receipt_store or runtime.ingress_receipt_store
        self.checkpoints = checkpoint_store or runtime.adapter_checkpoint_store

    # --- intake ------------------------------------------------------------

    async def ingest(
        self,
        envelope: IngressEnvelope,
        *,
        checkpoint: AdapterCheckpoint | None = None,
        deliver: bool = True,
    ) -> IngressResult:
        """Take one envelope in, and (unless ``deliver`` is false) run the system.

        Args:
            envelope: What an adapter observed.
            checkpoint: Observation position to advance in the *same*
                transaction as the receipt and the Event, so a crash can never
                leave the checkpoint ahead of what was actually ingested.
            deliver: Route the new Event and drain.  ``False`` ingests without
                running processes, which lets an adapter take a whole batch in
                before any of it is acted on.

        Returns:
            An :class:`IngressResult`; ``status`` is ACCEPTED, DUPLICATE or
            REJECTED.  A duplicate carries the *original* receipt and Event.
        """
        validation = validate_envelope(envelope)
        if not validation.ok:
            return self._reject(envelope, validation.reasons)

        existing = self.receipts.get_by_source_key(
            envelope.adapter_id, envelope.source_event_key
        )
        if existing is not None:
            return self._duplicate(envelope, existing, checkpoint)

        receipt = IngressReceipt.from_envelope(envelope, status=IngressStatus.ACCEPTED)
        event = self.to_event(envelope, receipt)
        receipt.event_id = event.id

        try:
            with self.db.atomic():
                self.receipts.insert(receipt)
                self.event_store.append(event)
                if checkpoint is not None:
                    self.checkpoints.save(checkpoint)
        except DuplicateIngress:
            # Lost a race against a concurrent delivery of the same occurrence;
            # the other one won and its Event is the real one.
            winner = self.receipts.get_by_source_key(
                envelope.adapter_id, envelope.source_event_key
            )
            if winner is not None:
                return self._duplicate(envelope, winner, checkpoint)
            raise

        logger.info(
            "ingress ACCEPTED adapter=%s source_event_key=%s event=%s receipt=%s",
            envelope.adapter_id,
            envelope.source_event_key,
            event.id,
            receipt.id,
        )

        if deliver:
            await self.runtime.deliver_event(event)
        return IngressResult(status=IngressStatus.ACCEPTED, receipt=receipt, event=event)

    async def ingest_batch(
        self, envelopes: list[IngressEnvelope], *, deliver: bool = True
    ) -> list[IngressResult]:
        """Ingest several envelopes in order, returning one result each."""
        results = []
        for envelope in envelopes:
            results.append(await self.ingest(envelope, deliver=deliver))
        return results

    # --- conversion --------------------------------------------------------

    def to_event(self, envelope: IngressEnvelope, receipt: IngressReceipt) -> Event:
        """Build the raw Event an accepted envelope becomes (spec §14).

        The Event stays an ordinary Event: same type, same payload shape, no
        ingress fields smuggled into the payload.  Its origin is carried by
        ``ingress_receipt_id``, which is where provenance queries look.
        """
        return Event(
            type=envelope.event_type,
            source=envelope.adapter_id,
            payload=dict(envelope.payload),
            occurred_at=envelope.observed_at,
            ingress_receipt_id=receipt.id,
        )

    # --- outcomes ----------------------------------------------------------

    def _reject(self, envelope: IngressEnvelope, reasons: list[str]) -> IngressResult:
        """Refuse an unusable envelope; record it when it can be keyed."""
        receipt = None
        if is_keyable(envelope):
            receipt = IngressReceipt.from_envelope(
                envelope, status=IngressStatus.REJECTED
            )
            receipt.reasons = list(reasons)
            try:
                self.receipts.insert(receipt)
            except DuplicateIngress:
                # Already known under that identity — keep the earlier record.
                receipt = self.receipts.get_by_source_key(
                    envelope.adapter_id, envelope.source_event_key
                )
        logger.warning(
            "ingress REJECTED adapter=%s source_event_key=%s reasons=%s",
            envelope.adapter_id,
            envelope.source_event_key,
            reasons,
        )
        return IngressResult(
            status=IngressStatus.REJECTED, receipt=receipt, reasons=list(reasons)
        )

    def _duplicate(
        self,
        envelope: IngressEnvelope,
        existing: IngressReceipt,
        checkpoint: AdapterCheckpoint | None,
    ) -> IngressResult:
        """Report a redelivery: no new Event, no new work, no new action."""
        # The checkpoint still advances — we *have* now observed past this
        # point, even though it produced nothing new.
        if checkpoint is not None:
            self.checkpoints.save(checkpoint)
        logger.info(
            "ingress DUPLICATE adapter=%s source_event_key=%s -> receipt=%s",
            envelope.adapter_id,
            envelope.source_event_key,
            existing.id,
        )
        event = (
            self.event_store.get(existing.event_id) if existing.event_id else None
        )
        return IngressResult(
            status=IngressStatus.DUPLICATE, receipt=existing, event=event
        )

    # --- checkpoints (for adapters) ---------------------------------------

    def get_checkpoint(self, adapter_id: str, stream_key: str) -> AdapterCheckpoint | None:
        """Return an adapter's observation position for one stream."""
        return self.checkpoints.get(adapter_id, stream_key)

    def get_cursors(self, adapter_id: str) -> dict[str, str | None]:
        """Return ``{stream_key: cursor}`` for every stream of ``adapter_id``."""
        return self.checkpoints.cursors_for_adapter(adapter_id)

    def save_checkpoint(self, checkpoint: AdapterCheckpoint) -> AdapterCheckpoint:
        """Advance a checkpoint outside an ingest (e.g. after a no-op scan)."""
        return self.checkpoints.save(checkpoint)


class AdapterRegistry:
    """``adapter_id -> adapter``, for driving adapters by name (spec §47).

    A plain dict with a nicer error message.  No discovery, no plugin loading,
    no relation to a capability registry.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, object] = {}

    def register(self, adapter: object) -> object:
        """Register ``adapter`` under its own ``adapter_id``."""
        adapter_id = getattr(adapter, "adapter_id", None)
        if not adapter_id:
            raise ValueError("adapter must expose a non-empty adapter_id")
        self._adapters[adapter_id] = adapter
        logger.info("registered adapter %s (%s)", adapter_id, type(adapter).__name__)
        return adapter

    def get(self, adapter_id: str) -> object:
        """Return the adapter registered under ``adapter_id``."""
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise KeyError(f"no adapter registered for {adapter_id!r}") from exc

    def __contains__(self, adapter_id: object) -> bool:
        return adapter_id in self._adapters

    def __iter__(self):
        return iter(self._adapters.values())

    @property
    def ids(self) -> list[str]:
        """Registered adapter ids, in registration order."""
        return list(self._adapters)
