"""External adapters — the boundary layer that watches the world.

Three shapes ship in Phase 3D: a manual/CLI adapter, a generic webhook adapter
and a local file observation adapter.  Mail, Slack, GitHub and browser sources
are deliberately not here yet.
"""

from .base import AdapterError, ExternalAdapter, PullAdapter, PushAdapter
from .manual import ManualAdapter
from .webhook import WebhookAdapter, WebhookIngress, WebhookResponse, WebhookServer
from .file_watch import (
    FILE_CREATED,
    FILE_DELETED,
    FILE_MODIFIED,
    LocalFileAdapter,
    content_fingerprint,
)

__all__ = [
    "AdapterError",
    "ExternalAdapter",
    "PullAdapter",
    "PushAdapter",
    "ManualAdapter",
    "WebhookAdapter",
    "WebhookIngress",
    "WebhookResponse",
    "WebhookServer",
    "LocalFileAdapter",
    "FILE_CREATED",
    "FILE_MODIFIED",
    "FILE_DELETED",
    "content_fingerprint",
]
