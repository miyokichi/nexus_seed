"""Local file observation adapter — notices that files changed, nothing more.

The discipline that matters here is what this adapter *refuses* to do
(spec §34 / Invariant 34).  It reports that ``data/input.xlsx`` now has a
different content hash.  It does not open the workbook, does not extract
figures, does not decide what changed about the world.  Interpretation is the
perception pipeline's job, where it is proposed, validated and auditable — an
adapter that quietly parsed spreadsheets would be an unreviewable back door
into World State.

Identity (spec §35): a file version is ``path + change type + content hash``.
Hashing rather than trusting mtime means a touch, a copy, a clock skew or a
restart cannot manufacture a change that did not happen — and a genuine edit
that happens to preserve mtime is still seen.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..ingress.models import AdapterCheckpoint, IngressEnvelope
from ....resources.scope import ResourceScope
from .base import AdapterError

logger = logging.getLogger("nexus_seed.adapters.file")

DEFAULT_ADAPTER_ID = "local_file"

#: Event types this adapter can raise.
FILE_CREATED = "file_created"
FILE_MODIFIED = "file_modified"
FILE_DELETED = "file_deleted"

#: Cursor value recorded for a path once it is gone.
DELETED_CURSOR = "deleted"


def content_fingerprint(path: Path) -> str:
    """Return ``sha256-<hex>`` for a file's contents.

    Phase 3D assumes modest files and hashes them whole; a large-file strategy
    (size + head/tail sampling, or an OS change journal) is a later concern.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256-{digest.hexdigest()}"


class LocalFileAdapter:
    """Polls a sandboxed directory tree and reports content-level changes.

    Polling, not an OS watcher (spec §31, §76): ``poll()`` is called explicitly,
    which keeps tests deterministic and leaves daemonisation for later without
    changing this contract.
    """

    source_type = "file"

    def __init__(
        self,
        allowed_root: str | Path,
        *,
        adapter_id: str = DEFAULT_ADAPTER_ID,
        patterns: tuple[str, ...] = ("**/*",),
        detect_deletions: bool = True,
    ) -> None:
        # Phase 3E: read-only scope.  A watcher observes; it has no business
        # writing into what it watches, and the type now says so.
        self.scope = ResourceScope.read_only(allowed_root)
        self.allowed_root = self.scope.read_roots[0]
        self._adapter_id = adapter_id
        self.patterns = patterns
        self.detect_deletions = detect_deletions
        #: Set by :meth:`bind`; where checkpoints are read from and written to.
        self.checkpoints = None

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def bind(self, ingress_service) -> "LocalFileAdapter":
        """Attach the ingress service this adapter reads checkpoints from."""
        self.checkpoints = ingress_service
        return self

    # --- sandbox -----------------------------------------------------------

    def within_sandbox(self, path: Path) -> bool:
        """Whether ``path`` really lives under ``allowed_root``.

        Delegates to the shared :class:`ResourceScope`, which resolves first so
        a symlink pointing outside the tree is caught rather than followed
        (spec §38).
        """
        return self.scope.can_read(path)

    # --- observation -------------------------------------------------------

    def scan(self) -> dict[str, str]:
        """Return ``{relative path: fingerprint}`` for everything visible now."""
        seen: dict[str, str] = {}
        for pattern in self.patterns:
            for path in sorted(self.allowed_root.glob(pattern)):
                if not path.is_file() or not self.within_sandbox(path):
                    continue
                try:
                    seen[self.stream_key(path)] = content_fingerprint(path)
                except OSError as exc:
                    # One unreadable file must not blind the whole scan.
                    logger.warning("skipping unreadable file %s: %s", path, exc)
        return seen

    def stream_key(self, path: Path) -> str:
        """The normalized, root-relative identity of a file."""
        return self.scope.relative_key(path)

    async def poll(self) -> list[IngressEnvelope]:
        """Return an envelope for every file version not yet observed.

        Compares the current scan against the persisted checkpoints, so a
        restart resumes from what was actually ingested rather than treating
        every existing file as new (spec §40).
        """
        if self.checkpoints is None:
            raise AdapterError("adapter is not bound to an ingress service")

        known = self.checkpoints.get_cursors(self._adapter_id)
        current = self.scan()

        envelopes: list[IngressEnvelope] = []
        for stream_key, fingerprint in current.items():
            previous = known.get(stream_key)
            if previous == fingerprint:
                continue  # unchanged: nothing happened (spec §61)
            change_type = (
                FILE_CREATED
                if previous is None or previous == DELETED_CURSOR
                else FILE_MODIFIED
            )
            envelopes.append(self._envelope(stream_key, change_type, fingerprint))

        if self.detect_deletions:
            for stream_key, previous in known.items():
                if stream_key in current or previous == DELETED_CURSOR:
                    continue
                envelopes.append(
                    self._envelope(stream_key, FILE_DELETED, DELETED_CURSOR, previous=previous)
                )

        return envelopes

    def _envelope(
        self,
        stream_key: str,
        change_type: str,
        fingerprint: str,
        *,
        previous: str | None = None,
    ) -> IngressEnvelope:
        path = self.allowed_root / stream_key
        size, mtime = None, None
        if change_type != FILE_DELETED:
            try:
                stat = path.stat()
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            except OSError:  # vanished between scan and envelope; report anyway
                pass

        # A deleted file has no content to hash, so its identity leans on the
        # fingerprint it had when it was last seen (spec §37).
        identity = previous if change_type == FILE_DELETED else fingerprint
        return IngressEnvelope(
            adapter_id=self._adapter_id,
            source_type=self.source_type,
            source_event_key=f"file:{stream_key}:{change_type}:{identity}",
            event_type=change_type,
            payload={
                "path": stream_key,
                "change_type": change_type,
                "size": size,
                "mtime": mtime,
                "content_hash": None if change_type == FILE_DELETED else fingerprint,
            },
            source_cursor=fingerprint,
            metadata={"allowed_root": str(self.allowed_root), "previous": previous},
        )

    def checkpoint_for(self, envelope: IngressEnvelope) -> AdapterCheckpoint:
        """The checkpoint advance that goes with ``envelope``."""
        return AdapterCheckpoint(
            adapter_id=self._adapter_id,
            stream_key=envelope.payload["path"],
            cursor=envelope.source_cursor,
            metadata={"change_type": envelope.payload["change_type"]},
        )

    # --- driving -----------------------------------------------------------

    async def poll_and_ingest(self, ingress_service=None, *, deliver: bool = True) -> list:
        """Poll, then ingest each envelope with its checkpoint advance.

        The checkpoint moves inside the *same* transaction as the receipt and
        the Event, so a crash mid-batch can never leave the adapter believing it
        observed something that was never ingested.
        """
        service = ingress_service or self.checkpoints
        if service is None:
            raise AdapterError("adapter is not bound to an ingress service")

        results = []
        for envelope in await self.poll():
            results.append(
                await service.ingest(
                    envelope,
                    checkpoint=self.checkpoint_for(envelope),
                    deliver=deliver,
                )
            )
        return results
