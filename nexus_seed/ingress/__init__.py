"""Ingress boundary — how the outside world becomes an Event.

The inbound mirror of ``actions/``::

    External World -> Adapter -> IngressEnvelope -> validation -> dedup
                   -> IngressReceipt + Raw Event -> (existing perception)

Nothing here is a core primitive; these are domain models and one service that
adapters use, exactly like ``world/``, ``work/`` and ``actions/``.
"""

from .models import (
    AdapterCheckpoint,
    DuplicateIngress,
    IngressEnvelope,
    IngressReceipt,
    IngressResult,
    IngressStatus,
)
from .service import AdapterRegistry, IngressService
from .trace import IngressTrace, get_ingress_trace, get_ingress_trace_by_source_key
from .validation import EnvelopeValidation, is_keyable, validate_envelope

__all__ = [
    "AdapterCheckpoint",
    "AdapterRegistry",
    "DuplicateIngress",
    "EnvelopeValidation",
    "IngressEnvelope",
    "IngressReceipt",
    "IngressResult",
    "IngressService",
    "IngressStatus",
    "IngressTrace",
    "get_ingress_trace",
    "get_ingress_trace_by_source_key",
    "is_keyable",
    "validate_envelope",
]
