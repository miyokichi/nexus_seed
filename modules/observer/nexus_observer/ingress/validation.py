"""Envelope validation — the first gate on anything arriving from outside.

Deliberately structural only (spec §13).  Ingress checks that an envelope is
*usable*, never that its content is *true* — judging meaning is what the
perception pipeline (Phase 3B) exists for, and doing it here would put domain
knowledge in an infrastructure boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import IngressEnvelope


@dataclass
class EnvelopeValidation:
    """The outcome of validating one envelope."""

    ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        """Record a failure reason."""
        self.ok = False
        self.reasons.append(reason)


def validate_envelope(envelope: IngressEnvelope) -> EnvelopeValidation:
    """Check that ``envelope`` can safely become an Event."""
    result = EnvelopeValidation()

    if not envelope.adapter_id:
        result.fail("empty adapter_id")
    if not envelope.source_type:
        result.fail("empty source_type")
    if not envelope.source_event_key:
        # Without an external identity there is nothing to deduplicate on, so a
        # redelivery could not be recognised.  Refuse rather than ingest
        # something that can silently duplicate later.
        result.fail("empty source_event_key")
    if not envelope.event_type:
        result.fail("empty event_type")
    if not isinstance(envelope.payload, dict):
        result.fail("payload is not a dict")
    if not isinstance(envelope.metadata, dict):
        result.fail("metadata is not a dict")
    if not isinstance(envelope.observed_at, datetime):
        result.fail("observed_at is not a datetime")

    return result


def is_keyable(envelope: IngressEnvelope) -> bool:
    """Whether a receipt for ``envelope`` can be stored at all.

    A rejected envelope is still worth recording — unless it lacks the very
    identity the receipt table is keyed by, in which case there is nothing to
    store it under.
    """
    return bool(envelope.adapter_id and envelope.source_event_key)
