"""Source-specific external observation adapters."""

from .base import AdapterError, ExternalAdapter, PullAdapter, PushAdapter
from .file_watch import (
    FILE_CREATED,
    FILE_DELETED,
    FILE_MODIFIED,
    LocalFileAdapter,
    content_fingerprint,
)
from .manual import ManualAdapter

__all__ = [
    "AdapterError",
    "ExternalAdapter",
    "FILE_CREATED",
    "FILE_DELETED",
    "FILE_MODIFIED",
    "LocalFileAdapter",
    "ManualAdapter",
    "PullAdapter",
    "PushAdapter",
    "content_fingerprint",
]
