"""Manual adapter — a human or script hands NEXUS SEED one occurrence.

The smallest possible adapter, and the one that makes the boundary's rules
visible: even a human typing a message at a terminal must supply an
``source_event_key``, because "I meant to send this twice" and "my shell
re-ran the command" have to be distinguishable (spec §18, §25).
"""

from __future__ import annotations

from datetime import datetime

from ..core.event import utcnow
from ..ingress.models import IngressEnvelope

DEFAULT_ADAPTER_ID = "manual"


class ManualAdapter:
    """Builds envelopes from explicit, caller-supplied input.

    A push adapter with no transport: the caller *is* the source.  There is no
    ``poll`` because there is nothing to poll — the occurrence arrives when
    somebody decides it does.
    """

    source_type = "manual"

    def __init__(self, adapter_id: str = DEFAULT_ADAPTER_ID) -> None:
        self._adapter_id = adapter_id

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def envelope(
        self,
        *,
        event_type: str,
        source_event_key: str,
        payload: dict | None = None,
        observed_at: datetime | None = None,
        metadata: dict | None = None,
    ) -> IngressEnvelope:
        """Build an envelope for one manually-supplied occurrence.

        Args:
            event_type: The NEXUS Event type to raise.
            source_event_key: The caller's idempotency key.  Reusing it is how
                a caller says "this is the same occurrence I already told you
                about", and is the only protection against a re-run.
            payload: The Event payload.
            observed_at: When it happened; defaults to now.
            metadata: Extra audit context (who ran it, from where).
        """
        return IngressEnvelope(
            adapter_id=self._adapter_id,
            source_type=self.source_type,
            source_event_key=source_event_key,
            event_type=event_type,
            payload=dict(payload or {}),
            observed_at=observed_at or utcnow(),
            metadata=dict(metadata or {}),
        )
